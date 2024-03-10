"""Runs a pytorch server."""
import os
from typing import Sequence

from absl import app
from absl import flags

from jetstream.core import server_lib
from petstream.pets import config


_PORT = flags.DEFINE_integer('port', 9000, 'port to listen on')
_THREADS = flags.DEFINE_integer(
    'threads', 64, 'number of worker threads in thread pool'
)
_CONFIG = flags.DEFINE_string(
    'config',
    'InterleavedCPUTestServer',
    'available servers',
)

_TOKENIZER_PATH = flags.DEFINE_string(
    'tokenizer_path',
    'petstream/pets/tokenizer.model',
    'The tokenizer model path',
    required=False,
)
_CKPT_PATH = flags.DEFINE_string(
    'checkpoint_path', None, 'Directory for .pth checkpoints', required=False
)
_BF16_ENABLE = flags.DEFINE_bool(
    'bf16_enable', False, 'Whether to enable bf16', required=False
)
_CONTEXT_LENGTH = flags.DEFINE_integer(
    'context_length', 1024, 'The context length', required=False
)
_BATCH_SIZE = flags.DEFINE_integer(
    'batch_size', 32, 'The batch size', required=False
)
_PROFILING_OUTPUT =flags.DEFINE_string(
    'profiling_output',
    '',
    'The profiling output',
    required=False,
)
_PLATFORM =flags.DEFINE_string(
    'platform',
    'tpu=4',
    'The platform that the engine runs on',
    required=False,
)
_PARAM_SIZE =flags.DEFINE_string(
    'param_size',
    '7b',
    'The model size the server runs on.',
    required=False,
)

def main(argv: Sequence[str]):
  del argv
  os.environ['XLA_FLAGS'] = '--xla_dump_to=/tmp/xla_logs --xla_dump_hlo_as_text'
  # No devices for local cpu test. A None for prefill and a None for generate.
  devices = server_lib.get_devices()
  print('HERE 1')
  server_config = config.create_config(
        devices,
        tokenizer_path=_TOKENIZER_PATH.value,
        ckpt_path=_CKPT_PATH.value,
        bf16_enable=True,
        param_size=_PARAM_SIZE.value,
        context_length=_CONTEXT_LENGTH.value,
        batch_size=_BATCH_SIZE.value,
        platform=_PLATFORM.value,
  )
  print('HERE 2')

  # We separate credential from run so that we can unit test it with local credentials.
  # TODO: Add grpc credentials for OSS.
  jetstream_server = server_lib.run(
      threads=_THREADS.value,
      port=_PORT.value,
      config=server_config,
      devices=devices,
  )
  print('HANQ....')
  jetstream_server.wait_for_termination()


if __name__ == '__main__':
  app.run(main)
