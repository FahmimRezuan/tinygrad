from __future__ import annotations
import platform
import numpy as np
import pyopencl as cl  # type: ignore
from typing import Optional, List
from tinygrad.helpers import DEBUG, getenv, prod, ImageDType
from tinygrad.ops import Compiled
from tinygrad.runtime.lib import RawBufferCopyInOut
from tinygrad.codegen.gpu import GPUCodegen, GPULanguage

OSX = platform.system() == "Darwin"
OSX_TIMING_RATIO = (125/3) if OSX else 1.0   # see test/external_osx_profiling.py to determine this ratio. it's in like GPU clocks or something
FLOAT16 = getenv("FLOAT16", 0)

class _CL:
  def __init__(self):
    devices: List[cl.Device] = sum([x.get_devices(device_type=cl.device_type.GPU) for x in cl.get_platforms()], [])
    if len(devices) == 0: devices = sum([x.get_devices(device_type=cl.device_type.CPU) for x in cl.get_platforms()], []) # settle for CPU
    if len(devices) > 1 or DEBUG >= 1: print(f"using {devices[getenv('CL_DEVICE', 0)]}")
    self.cl_ctx: cl.Context = cl.Context(devices=[devices[getenv("CL_DEVICE", 0)]])
    self.cl_queue: cl.CommandQueue = cl.CommandQueue(self.cl_ctx, properties=cl.command_queue_properties.PROFILING_ENABLE)  # this is an in-order command queue
CL = _CL()

# TODO: merge CLImage in here
class CLBuffer(RawBufferCopyInOut):
  def __init__(self, size, dtype):
    if isinstance(dtype, ImageDType):
      fmt = cl.ImageFormat(cl.channel_order.RGBA, {2: cl.channel_type.HALF_FLOAT, 4: cl.channel_type.FLOAT}[dtype.itemsize])
      buf = cl.Image(CL.cl_ctx, cl.mem_flags.READ_WRITE, fmt, shape=(dtype.shape[1], dtype.shape[0]))
      assert size == prod(dtype.shape), f"image size mismatch {size} != {dtype.shape}"
      # NOTE: the memory is a bit off here due to padding, it's buf.row_pitch * buf.height * 4 * dtype.itemsize
    else:
      buf = cl.Buffer(CL.cl_ctx, cl.mem_flags.READ_WRITE, size * dtype.itemsize)
    super().__init__(size, dtype, buf)
  def _copyin(self, x:np.ndarray):
    assert not self.dtype.name.startswith("image"), f"can't copyin images {self.dtype}"
    cl.enqueue_copy(CL.cl_queue, self._buf, x, is_blocking=False)
  def _copyout(self, x:np.ndarray):
    assert not self.dtype.name.startswith("image"), f"can't copyout images {self.dtype}"
    cl.enqueue_copy(CL.cl_queue, x, self._buf, is_blocking=True)

class CLProgram:
  def __init__(self, name:str, prg:str, binary=False, argdtypes=None):
    self.name, self.argdtypes, self.clprogram = name, argdtypes, cl.Program(CL.cl_ctx, CL.cl_ctx.devices, [prg]) if binary else cl.Program(CL.cl_ctx, prg)  # type: ignore
    try:
      self._clprg = self.clprogram.build()
    except cl.RuntimeError as e:
      if DEBUG >= 3: print("FAILED TO BUILD", prg)
      raise e
    self.clprg = self._clprg.__getattr__(name)
    if DEBUG >= 5 and not OSX:
      binary = self.clprogram.get_info(cl.program_info.BINARIES)[0]
      if 'Adreno' in CL.cl_ctx.devices[0].name:
        from disassemblers.adreno import disasm
        disasm(binary)
      else:
        # print the PTX for NVIDIA. TODO: probably broken for everything else
        print(binary.decode('utf-8'))
    if self.argdtypes is not None: self.clprg.set_scalar_arg_dtypes(self.argdtypes)

  @staticmethod
  def max_work_group_size(): return CL.cl_ctx.devices[0].max_work_group_size

  def __call__(self, global_size, local_size, *bufs, wait=False) -> Optional[float]:
    e = self.clprg(CL.cl_queue, global_size, local_size, *[x._buf if isinstance(x, CLBuffer) else x for x in bufs])
    if wait:
      CL.cl_queue.finish()
      return ((e.profile.end - e.profile.start) * OSX_TIMING_RATIO) * 1e-9
    return None

class CLCodegen(GPUCodegen):
  lang = GPULanguage(
    kernel_prefix = "__kernel", buffer_prefix = "__global ", smem_prefix = "__local ",
    half_prekernel = "#pragma OPENCL EXTENSION cl_khr_fp16 : enable",
    barrier = "barrier(CLK_LOCAL_MEM_FENCE);", float4 = "(float4)",
    gid = [f'get_global_id({i})' for i in range(3)], lid = [f'get_local_id({i})' for i in range(3)])

GPUBuffer = Compiled(CLBuffer, CLCodegen, CLProgram)
