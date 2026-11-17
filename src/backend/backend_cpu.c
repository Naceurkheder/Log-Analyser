/* backend_cpu.c — swappable backend binding for libsentinel_cpu.so. */
#include "sentinel/sentinel.h"

int sentinel_detect(const sentinel_input* in, const sentinel_config* cfg,
                    sentinel_results* out) {
    return sentinel_detect_cpu(in, cfg, out);
}
