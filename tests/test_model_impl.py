from jetstream_pt.third_party.llama2 import model_exportable
from jetstream_pt.third_party.llama2 import model_original
from jetstream_pt import layers 
from jetstream_pt.third_party.llama2 import model_args
from jetstream_pt import environment
from jetstream_pt import cache_manager
import torch
from torch.utils import _pytree as pytree
import torch_xla2
import jax
import jax.numpy as jnp

import unittest


class ModelComponentTest(unittest.TestCase):

    def setup(self):
        torch.set_default_dtype(torch.bfloat16)

    def _prefill_mask(self, seqlen, start_pos):
        mask = torch.full(
            (seqlen, seqlen), float("-inf")
        )

        mask = torch.triu(mask, diagonal=1)

        # When performing key-value caching, we compute the attention scores
        # only for the new sequence. Thus, the matrix of scores is of size
        # (seqlen, cache_len + seqlen), and the only masked entries are (i, j) for
        # j > cache_len + i, since row i corresponds to token cache_len + i.
        mask = torch.hstack([
            torch.zeros((seqlen, start_pos)),
            mask
        ])
        return mask
    
    def _make_freqs_cis(self, model_arg, seqlen, start_pos):
        freqs_cis = model_original.precompute_freqs_cis(
            # Note that self.params.max_seq_len is multiplied by 2 because the token limit for the Llama 2 generation of models is 4096. 
            # Adding this multiplier instead of using 4096 directly allows for dynamism of token lengths while training or fine-tuning.
            model_arg.dim // model_arg.n_heads, model_arg.max_seq_len * 2
        )
        freqs_cis = freqs_cis[start_pos : start_pos + seqlen]
        return freqs_cis

    def _to_xla_tensor(self, tree):
        return pytree.tree_map_only(
            torch.Tensor,
            torch_xla2.tensor.move_to_device, tree)

    def _make_env(self):
        env_data = environment.JetEngineEnvironmentData()
        env_data.max_input_sequence_length = 128
        env_data.max_input_sequence_length = 128
        env_data.cache_sequence_length = 128
        env_data.model_type = 'llama-2-tiny'
        env_data.batch_size = 1
        env = environment.JetEngineEnvironment(env_data)
        env.apply_sharding = lambda *args, **kwargs: None  # don't shard on cpu
        return env

    def _call_xla_model(self, model, weights, args):
        with jax.default_device(jax.devices('cpu')[0]):
            xla_weights, xla_inputs = self._to_xla_tensor(
                (weights, args)) 
            result = torch.func.functional_call(model, xla_weights, xla_inputs)
            result_torch = torch_xla2.tensor.j2t(result._elem)
            return result_torch

    def _generate_mask(self, cache_length, pos, seqlen):
        x = jnp.arange(0, cache_length)
        cond = jnp.logical_and(x <= pos, x >= pos - seqlen)
        res = jnp.where(cond, 0, float('-inf'))
        return torch_xla2.tensor.wrap(res)


    def _compare_cache(self, cache_torch, cache_jax):
        batch, seq, _, _ = cache_torch.shape
        cache_j = torch_xla2.tensor.j2t(cache_jax._elem)
        for s in range(seq):
            print('diff ', (cache_torch[0, s] - cache_j[0, :, s]).norm())

    def _make_one_cache_for_generate(self, env, pos):
        cache_array_k = jnp.zeros((1, env.num_heads, env.cache_sequence_length, env.head_dim))
        cache_array_v = jnp.zeros((1, env.num_heads, env.cache_sequence_length, env.head_dim))
        cache_array_k, cache_array_v = torch_xla2.tensor.wrap((cache_array_k, cache_array_v))
        cache_decode = cache_manager.KVCacheGenerate(cache_array_k, cache_array_v, pos, None)
        return cache_decode

    def test_attention(self):
        env = self._make_env()
        model_arg = env._model_arg 

        attention_orig = model_original.Attention(model_arg)
        attention_ours = layers.Attention(model_arg, env)

        seqlen = 32
        batch = 1
        x = torch.randn((batch, seqlen, model_arg.dim)) # (batch, seqlen, embedding dim)
        start_pos = 0
        freqs_cis = self._make_freqs_cis(model_arg, seqlen, start_pos)
        mask = self._prefill_mask(seqlen, start_pos)
        inputs_orig = (
            x,
            start_pos,
            freqs_cis,
            mask
        )

        expected_out = attention_orig(*inputs_orig)

        cache = cache_manager.KVCachePrefill()
        freqs_cis = freqs_cis.reshape(batch, seqlen, -1)
        input_ours = (
            x,
            freqs_cis,
            mask,
            cache,
        )

        result_torch = self._call_xla_model(
            attention_ours, attention_orig.state_dict(), input_ours)

        print('Single Attention: Diff norm', (result_torch - expected_out).norm())
        self.assertTrue(torch.allclose(result_torch, expected_out, atol=1e-4))


        pos = 32  # 
        cache_decode = self._make_one_cache_for_generate(env, pos)

        # insert prefilled cache entry
        cache_decode.cache_k._elem = cache_decode.cache_k._elem.at[:, :, :pos, :].set(cache.cache_k._elem)
        cache_decode.cache_v._elem = cache_decode.cache_v._elem.at[:, :, :pos, :].set(cache.cache_v._elem)

        # self._compare_cache(attention_orig.cache_k, cache_decode.cache_k)
        # Now do one with decode
        x2 = torch.randn((batch, 1, model_arg.dim))
        freqs_cis = self._make_freqs_cis(model_arg, 1, 32)
        inputs_orig2 = (
            x2, 
            pos,
            freqs_cis,
            None,  # mask is none for decode
        )
        expected_out = attention_orig(*inputs_orig2)
        cache_decode.pos = [pos]  # next position to update
        mask = self._generate_mask(env.cache_sequence_length, pos, seqlen)
        mask = mask.reshape(1,1,1, -1)  # seq dim is the last one
        freqs_cis = freqs_cis.reshape(batch, 1, -1)
        input_ours2 = (
            x2,
            freqs_cis,
            mask,
            cache_decode
        )
        result_torch = self._call_xla_model(
            attention_ours, attention_orig.state_dict(), input_ours2)

        print('Single Attention: decode diff norm', (result_torch - expected_out).norm())
        self.assertTrue(torch.allclose(result_torch, expected_out, atol=1e-4))

    def test_transformer_block(self):
        env = self._make_env()
        model_arg = env._model_arg 

        block_orig = model_original.TransformerBlock(0, model_arg)
        block_ours = model_exportable.TransformerBlock(0, model_arg, env)

        batch = 1
        seqlen = 32
        x = torch.randn((batch, seqlen, model_arg.dim)) # (batch, seqlen, embedding dim)
        start_pos = 0
        freqs_cis = self._make_freqs_cis(model_arg, seqlen, start_pos)
        mask = self._prefill_mask(seqlen, start_pos)
        inputs_orig = (
            x,
            start_pos,
            freqs_cis,
            mask
        )

        expected_out = block_orig(*inputs_orig)

        cache = cache_manager.KVCachePrefill()
        freqs_cis = freqs_cis.reshape(batch, seqlen, -1)
        input_ours = (
            x,
            freqs_cis,
            mask,
            cache,
        )

        result_torch = self._call_xla_model(
            block_ours, block_orig.state_dict(), input_ours)

        print('Single TransBlock: Diff norm', (result_torch - expected_out).norm())
        self.assertTrue(torch.allclose(result_torch, expected_out, atol=1e-4))


        pos = 32  # 
        cache_decode = self._make_one_cache_for_generate(env, pos)

        # insert prefilled cache entry
        cache_decode.cache_k._elem = cache_decode.cache_k._elem.at[:, :, :pos, :].set(cache.cache_k._elem)
        cache_decode.cache_v._elem = cache_decode.cache_v._elem.at[:, :, :pos, :].set(cache.cache_v._elem)

        # Now do one with decode
        x2 = torch.randn((1, 1, model_arg.dim))
        freqs_cis = self._make_freqs_cis(model_arg, 1, 32)
        inputs_orig2 = (
            x2, 
            pos,
            freqs_cis,
            None,  # mask is none for decode
        )
        expected_out = block_orig(*inputs_orig2)
        cache_decode.pos = [pos]  # next position to update
        mask = self._generate_mask(env.cache_sequence_length, pos, seqlen)
        mask = mask.reshape(1,1,1, -1)  # seq dim is the last one
        freqs_cis = freqs_cis.reshape(batch, 1, -1)
        input_ours2 = (
            x2,
            freqs_cis,
            mask,
            cache_decode
        )
        result_torch = self._call_xla_model(
            block_ours, block_orig.state_dict(), input_ours2)

        print('Single Attention: decode diff norm', (result_torch - expected_out).norm())
        self.assertTrue(torch.allclose(result_torch, expected_out, atol=1e-4))

    def test_transformer(self):
        env = self._make_env()
        model_arg = env._model_arg 

        model_orig = model_original.Transformer(model_arg)
        state_dict = dict(model_orig.state_dict())
        state_dict['freqs_cis'] = model_orig.freqs_cis
        model_ours = model_exportable.Transformer(model_arg, env)

        seqlen = 32
        x = torch.randint(0, 32000, (1, seqlen)) # (batch, seqlen, embedding dim)
        start_pos = 0
        mask = self._prefill_mask(seqlen, start_pos)
        inputs_orig = (
            x,
            start_pos
        )

        expected_out = model_orig(*inputs_orig)

        caches = env.make_caches_prefill()
        input_pos = torch.arange(0, seqlen)
        input_ours = (
            x,
            input_pos,
            caches,
            mask,
        )

        result_torch = self._call_xla_model(
            model_ours, state_dict, input_ours)

        print('Transformer: Diff norm', (result_torch - expected_out).norm())
        self.assertTrue(torch.allclose(result_torch, expected_out, atol=1e-4))
        return
        pos = 32  # 
        caches_decode = env.make_caches_generate()

        for cache_decode, cache in zip(caches_decode, caches):
            # insert prefilled cache entry
            cache_decode.cache_k._elem = cache_decode.cache_k._elem.at[:, :, :pos, :].set(cache.cache_k._elem)
            cache_decode.cache_v._elem = cache_decode.cache_v._elem.at[:, :, :pos, :].set(cache.cache_v._elem)

        # Now do one with decode
        x2 = torch.randn((1, 1, model_arg.dim))
        freqs_cis = self._make_freqs_cis(model_arg, 1, 32)
        inputs_orig2 = (
            x2, 
            pos,
            freqs_cis,
            None,  # mask is none for decode
        )
        expected_out = block_orig(*inputs_orig2)
        cache_decode.pos = [pos]  # next position to update
        mask = self._generate_mask(env.cache_sequence_length, pos, seqlen)
        mask = mask.reshape(1,1,1, -1)  # seq dim is the last one
        input_ours2 = (
            x2,
            freqs_cis,
            mask,
            cache_decode
        )
        result_torch = self._call_xla_model(
            block_ours, block_orig.state_dict(), input_ours2)

        print('Single Attention: decode diff norm', (result_torch - expected_out).norm())
        self.assertTrue(torch.allclose(result_torch, expected_out, atol=1e-4))


if __name__ == '__main__':
    unittest.main()

