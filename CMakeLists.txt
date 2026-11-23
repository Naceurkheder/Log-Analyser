cmake_minimum_required(VERSION 3.20)
project(sentinel-gpu VERSION 0.5.0 LANGUAGES C CXX)

# Sprint 5 / S5-A: one source tree, two shared libraries.
#   -DSENTINEL_ENABLE_CUDA=ON  (default) -> sentinel_cuda + sentinel_cpu + smoke
#   -DSENTINEL_ENABLE_CUDA=OFF          -> sentinel_cpu only (no CUDA toolkit)
option(SENTINEL_ENABLE_CUDA "Build the CUDA backend and smoke test" ON)

find_package(Git QUIET)
if(GIT_FOUND)
  execute_process(
    COMMAND ${GIT_EXECUTABLE} rev-parse --short HEAD
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
    OUTPUT_VARIABLE SENTINEL_GIT_HASH
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET
  )
endif()
if(NOT SENTINEL_GIT_HASH)
  set(SENTINEL_GIT_HASH "dev")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Keep the public header's ABI identity stable across both build systems.
add_compile_definitions(SENTINEL_BUILD_ID="${SENTINEL_GIT_HASH}")

add_library(sentinel_cpu SHARED
  src/common/tokenizer.cpp
  src/common/abi.cpp
  src/cpu/detect_cpu.cpp
  src/backend/backend_cpu.c
)
target_include_directories(sentinel_cpu PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/include
  ${CMAKE_CURRENT_SOURCE_DIR}/src/common
)
set_target_properties(sentinel_cpu PROPERTIES
  OUTPUT_NAME sentinel_cpu
  C_VISIBILITY_PRESET default
  CXX_VISIBILITY_PRESET default
)

if(SENTINEL_ENABLE_CUDA)
  # nvcc is often installed outside PATH (/usr/local/cuda-12.4/bin). CUDA 12.4
  # also rejects GCC 15 headers, so pin the validated host compiler when present.
  if(NOT CMAKE_CUDA_COMPILER)
    find_program(SENTINEL_NVCC NAMES nvcc PATHS
      /usr/local/cuda/bin /usr/local/cuda-12.4/bin /usr/local/cuda-12/bin)
    if(SENTINEL_NVCC)
      set(CMAKE_CUDA_COMPILER ${SENTINEL_NVCC} CACHE FILEPATH "CUDA compiler" FORCE)
    endif()
  endif()
  if(NOT CMAKE_CUDA_HOST_COMPILER)
    find_program(SENTINEL_HOST_CXX NAMES g++-13 g++-14 g++)
    if(SENTINEL_HOST_CXX)
      set(CMAKE_CUDA_HOST_COMPILER ${SENTINEL_HOST_CXX} CACHE STRING "CUDA host compiler" FORCE)
    endif()
  endif()
  # glibc >= 2.41 exposes C23 cospi/sinpi/rsqrt prototypes that clash with
  # CUDA 12.4; this must be in place before CMake's compiler-id probe.
  set(CMAKE_CUDA_FLAGS "${CMAKE_CUDA_FLAGS} -U_GNU_SOURCE -D_DEFAULT_SOURCE")

  include(CheckLanguage)
  check_language(CUDA)
  if(NOT CMAKE_CUDA_COMPILER)
    message(FATAL_ERROR
      "CUDA requested but no nvcc found; configure with -DSENTINEL_ENABLE_CUDA=OFF")
  endif()
  enable_language(CUDA)
  find_package(CUDAToolkit REQUIRED)

  # Validated host/container environment: CUDA 12.4 + sm_86 (RTX 3050 Laptop).
  # Override for another GPU with -DCMAKE_CUDA_ARCHITECTURES=<arch>.
  if(NOT CMAKE_CUDA_ARCHITECTURES)
    set(CMAKE_CUDA_ARCHITECTURES 86)
  endif()
  set(CMAKE_CUDA_STANDARD 17)
  set(CMAKE_CUDA_STANDARD_REQUIRED ON)
  add_library(sentinel_cuda SHARED
    src/common/tokenizer.cpp
    src/common/abi.cpp
    src/cpu/detect_cpu.cpp
    src/cuda/detect_gpu.cu
    src/backend/backend_gpu.c
  )
  target_include_directories(sentinel_cuda PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}/src/common
  )
  target_link_libraries(sentinel_cuda PRIVATE CUDA::cudart_static)
  set_target_properties(sentinel_cuda PROPERTIES OUTPUT_NAME sentinel)

  add_executable(sentinel_cuda_smoke tests/cuda/smoke.cu)
  target_link_libraries(sentinel_cuda_smoke PRIVATE CUDA::cudart_static)
  target_include_directories(sentinel_cuda_smoke PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
  target_sources(sentinel_cuda_smoke PRIVATE src/common/abi.cpp)
endif()
