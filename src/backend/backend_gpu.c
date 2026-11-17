/* backend_gpu.c — swappable backend binding for libsentinel.so (CUDA). */
#include "sentinel/sentinel.h"

int sentinel_detect(const sentinel_input* in, const sentinel_config* cfg,
                    sentinel_results* out) {
    return sentinel_detect_gpu(in, cfg, out);
}
