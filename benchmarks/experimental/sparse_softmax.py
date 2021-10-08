# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import contextlib
from pprint import pprint
import time

import torch
from torch.cuda import Event

from fairscale.experimental.nn import (  # noqa: F401
    BaselineSoftmax,
    InplaceSoftmax,
    TiledSoftmax,
    TopKFaissSoftmax,
    TopKSoftmax,
    TopKTiledSoftmax,
    TritonSoftmax,
)
from fairscale.experimental.nn.sparse_softmax import get_data
from fairscale.utils.testing import get_smi_memory

""" Benchmarking various softmax kernels. Some are dense and some are with label and top-K sparsity. """

# TODO:
#   From Naman: d_model varies between [2k, 12k]
#               input: 8 * 2K = 16K -- 2K is seq len, 8 is batch size
#               vocab: 256K
SHAPES = [
    # name, activation, FC weights
    ("1k_128h_256k", (1024, 128), (128, 256 * 1024)),
    ("4k_128h_256k", (4096, 128), (128, 256 * 1024)),
    # ("8k_4k_256k", (4*2048, 4*1024), (4*1024, 256 * 1024)),
    # ("8k_4k_256k", (4*2048, 4*1024), (4*1024, 256008)),
]
KERNELS = [
    BaselineSoftmax,
    #    TritonSoftmax,
    #    InplaceSoftmax,
    TiledSoftmax,
    #    TopKSoftmax,
    TopKTiledSoftmax,
    #    TopKFaissSoftmax,
]


def run_on_gpu(kernel, data, repeats, no_grad):
    input, weight, target = data

    # Ensure GPU memory is minimal and get_smi_memory is good
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    cur_mem_before = round(torch.cuda.memory_allocated() / 1024 / 1024)
    smi_mem_before = get_smi_memory()
    assert cur_mem_before == 0, cur_mem_before

    # Move tensors to GPU.
    input, weight.data, target = input.cuda(), weight.data.cuda(), target.cuda()

    # Create the kernel
    k = kernel(weight, 200)

    # Get the events
    events = [Event(enable_timing=True) for _ in range(repeats)]

    # Queue the ops to GPU
    cpu_start_time = time.time()
    for i in range(repeats):
        context = contextlib.suppress()
        if no_grad:
            context = torch.no_grad()
        with context:
            events[i].record()
            out = k(input, target)
            del out
    # Cpu is done
    cpu_time = time.time() - cpu_start_time
    # Might wait for gpu here
    torch.cuda.synchronize()

    # Get the durations
    durations = [cpu_time * 1000]  # convert seconds to ms.
    for x, y in zip(events, events[1:]):
        durations.append(x.elapsed_time(y))

    # Free memory
    del k
    input, weight.data, target = input.cpu(), weight.data.cpu(), target.cpu()
    weight.grad = None
    cur_mem_after = round(torch.cuda.memory_allocated() / 1024 / 1024)
    assert cur_mem_after == 0, cur_mem_after

    # Get peak mem
    peak_mem_after = round(torch.cuda.max_memory_allocated() / 1024 / 1024)
    smi_mem_after = get_smi_memory()
    peak_mem = peak_mem_after - cur_mem_before
    smi_peak_mem = smi_mem_after - smi_mem_before

    return peak_mem, smi_peak_mem, durations


def main():
    parser = argparse.ArgumentParser("Benchmarking softmax kernels")

    parser.add_argument("--dtype", type=str, choices=["fp16", "fp32"], default="fp16")
    parser.add_argument("--grad", type=str, choices=["grad", "no_grad"], default="grad")
    args = parser.parse_args()

    repeats = 9
    results = {}
    results["peak cached"] = {}
    results["peak smi"] = {}
    results["durations"] = {}
    for shape in SHAPES:
        name = shape[0]
        results["peak cached"][name] = {}
        results["peak smi"][name] = {}
        results["durations"][name] = {}
        dtype = torch.float32 if args.dtype == "fp32" else torch.float16
        data = get_data(shape[1:], dtype, "cpu")  # Use cpu memory to ensure we always start with empty GPU
        for kernel in KERNELS:
            k_name = kernel.__name__
            no_grad = args.grad
            print(f"Running {k_name} with {name} {dtype} {no_grad} data")
            peak_mem, smi_peak_mem, durations = run_on_gpu(kernel, data, repeats, no_grad == "no_grad")
            results["peak cached"][name][k_name] = peak_mem
            results["peak smi"][name][k_name] = smi_peak_mem
            results["durations"][name][k_name] = durations
    pprint(results)


if __name__ == "__main__":
    main()