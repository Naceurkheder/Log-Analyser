/* Build/ABI identity exported by both CPU-only and CUDA libraries. */

#include "sentinel/sentinel.h"

#ifndef SENTINEL_BUILD_ID
#define SENTINEL_BUILD_ID "dev"
#endif

extern "C" uint32_t sentinel_abi_version(void) {
    return SENTINEL_ABI_VERSION;
}

extern "C" const char* sentinel_build_id(void) {
    return SENTINEL_BUILD_ID;
}
