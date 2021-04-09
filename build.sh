#!/usr/bin/env bash

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ_DIR=$DIR

MODE="release"
PROTOBUF_GENERATE_PYTHON=1
PROTOBUF_GENERATE_CPP=1
CLEAN_BUILD_DIR=0

# Install protobuf dependencies before running this script.
# https://grpc.io/docs/languages/cpp/basics/
# https://grpc.io/docs/languages/python/quickstart/


_USAGE="

  $(basename "$0") [options]

Options
  -h, --help              Print usage
  -d, --debug             Set CMake build type to Debug  (default: Release)
  -c, --clean                 Remove cmake-build-* before running CMake.
  --no-protoc             Skip protobuf code generation
  --no-protoc-python      Skip Python protobuf code generation only
  --no-protoc-cpp         Skip C++ protobuf code generation only
"
while test $# -gt 0; do
  case "$1" in
    -h|--help)
      printf "Usage%s" "${_USAGE}"
      exit 0
      ;;
    -c|--clean)
      CLEAN_BUILD_DIR=1
      shift
      ;;
    -d|--debug)
      MODE="debug"
      shift
      ;;
    --no-protoc)
      PROTOBUF_GENERATE_PYTHON=0
      PROTOBUF_GENERATE_CPP=0
      shift
      ;;
    --no-protoc-python)
      PROTOBUF_GENERATE_PYTHON=0
      shift
      ;;
    --no-protoc-cpp)
      PROTOBUF_GENERATE_CPP=0
      shift
      ;;
    *)
      echo "Invalid option: $1" >&2
      printf "\nUsage%s" "${_USAGE}"
      exit 1;
      ;;
  esac
done



cat << EOF
CMAKE_BUILD_TYPE=$MODE
PROTOBUF_GENERATE_PYTHON=$PROTOBUF_GENERATE_PYTHON
PROTOBUF_GENERATE_CPP=$PROTOBUF_GENERATE_CPP
CLEAN_BUILD_DIR=$CLEAN_BUILD_DIR

EOF

set -ex

cd "$PROJ_DIR"


if [ $PROTOBUF_GENERATE_CPP = 1 ]; then
mkdir -p generated/proto
protoc -I. --grpc_out=generated/proto --plugin=protoc-gen-grpc="$(which grpc_cpp_plugin)" distlock.proto
protoc -I. --cpp_out=generated/proto distlock.proto
fi


if [ $PROTOBUF_GENERATE_PYTHON = 1 ]; then
mkdir -p generated/proto
python -m grpc_tools.protoc -I. --python_out=generated/proto --grpc_python_out=generated/proto distlock.proto
fi


if [ $CLEAN_BUILD_DIR = 1 ]; then
mkdir -p generated/proto
rm -r "cmake-build-${MODE}"
fi


if [ $MODE = "debug" ]; then
  mkdir -p cmake-build-debug
  cmake -H. -Bcmake-build-debug -DCMAKE_BUILD_TYPE=Debug
  make -Ccmake-build-debug -j 10

  ls -lah cmake-build-debug
  set +ex
  ldd cmake-build-debug/distlock
elif [ $MODE = "release" ]; then
  mkdir -p cmake-build-release
  cmake -H. -Bcmake-build-release -Dtest=OFF -DDEBUG=OFF -DCMAKE_BUILD_TYPE=Release
  make -Ccmake-build-release -j 10

  ls -lah cmake-build-release
  set +ex
  ldd cmake-build-release/distlock
fi

echo "OK"
