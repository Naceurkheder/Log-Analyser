/* Minimal CUDA toolchain/build smoke test for the Sprint 5 CMake target.
 * It proves nvcc can compile+link a kernel, cudaGetLastError sees launch
 * errors, and the exported ABI identity is linked into a CUDA binary. */
#include "sentinel/sentinel.h"

#include <cuda_runtime.h>
#include <cstdio>

__global__ void sentinel_smoke_add_one(int* values, int count) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) values[i] += 1;
}

int main() {
    constexpr int kCount = 1024;
    int* device = nullptr;
    if (cudaMalloc(&device, sizeof(int) * kCount) != cudaSuccess) {
        fprintf(stderr, "FAIL cudaMalloc\n");
        return 1;
    }
    int host[kCount];
    for (int i = 0; i < kCount; ++i) host[i] = i;
    if (cudaMemcpy(device, host, sizeof(host), cudaMemcpyHostToDevice) != cudaSuccess) {
        cudaFree(device);
        fprintf(stderr, "FAIL H2D\n");
        return 1;
    }
    sentinel_smoke_add_one<<<(kCount + 255) / 256, 256>>>(device, kCount);
    cudaError_t launch_err = cudaGetLastError();
    if (launch_err != cudaSuccess) {
        cudaFree(device);
        fprintf(stderr, "FAIL launch: %s\n", cudaGetErrorString(launch_err));
        return 1;
    }
    if (cudaMemcpy(host, device, sizeof(host), cudaMemcpyDeviceToHost) != cudaSuccess) {
        cudaFree(device);
        fprintf(stderr, "FAIL D2H\n");
        return 1;
    }
    cudaFree(device);
    for (int i = 0; i < kCount; ++i) {
        if (host[i] != i + 1) {
            fprintf(stderr, "FAIL value at %d\n", i);
            return 1;
        }
    }
    if (sentinel_abi_version() != SENTINEL_ABI_VERSION) {
        fprintf(stderr, "FAIL ABI version\n");
        return 1;
    }
    printf("PASS CUDA smoke (ABI v%u, build %s)\n",
           sentinel_abi_version(), sentinel_build_id());
    return 0;
}
