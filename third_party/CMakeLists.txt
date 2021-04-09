# Adds a static library named `third_party`

# Follow the instructions here and build gRPC locally:
#    https://grpc.io/docs/languages/cpp/quickstart/#build-and-install-grpc-and-protocol-buffers
# Then set GRPC_ROOT to whatever CMAKE_INSTALL_PREFIX was.
#    e.g. Pass -DGRPC_ROOT="$HOME/.local" to cmake in build.sh.
# Otherwise it will default to ~/usr
if(NOT DEFINED GRPC_ROOT)
    set(GRPC_ROOT $ENV{HOME}/usr)
else()
    set(GRPC_ROOT $CACHE{GRPC_ROOT})
endif()


if(NOT EXISTS ${GRPC_ROOT})
    message(SEND_ERROR "${GRPC_ROOT} does not exist") 
endif()


set(CMAKE_MODULE_PATH ${CMAKE_MODULE_PATH} "${CMAKE_SOURCE_DIR}/third_party")

set(LEVELDB_USE_STATIC_LIBS ON)
set(BUILD_SHARED_LIBS OFF)

find_package(LevelDB REQUIRED)
find_package(Threads REQUIRED)


find_package(Protobuf 3.2.0 CONFIG REQUIRED PATHS GRPC_ROOT)  # 3.2.0 is the minimum version.
find_package(gRPC CONFIG REQUIRED PATHS GRPC_ROOT)
find_package(re2 CONFIG REQUIRED PATHS GRPC_ROOT)

message(STATUS "Protobuf_VERSION: ${Protobuf_VERSION}")

add_library(cpp_gsl INTERFACE)
target_include_directories(cpp_gsl BEFORE INTERFACE
        ${CMAKE_SOURCE_DIR}/third_party/repos/GSL/include
        )

add_library(cxxopts INTERFACE)
target_include_directories(cxxopts BEFORE INTERFACE
        ${CMAKE_SOURCE_DIR}/third_party/repos/cxxopts/include
        )

add_library(spdlog INTERFACE)
target_include_directories(spdlog BEFORE INTERFACE
        ${CMAKE_SOURCE_DIR}/third_party/repos/spdlog/include
        )
target_link_libraries(spdlog INTERFACE Threads::Threads)

add_library(third_party INTERFACE)
target_include_directories(third_party INTERFACE
        ${PROTOBUF_INCLUDE_DIRS}
        )
target_link_libraries(third_party INTERFACE
        # Header-only libraries in git submodules
        cpp_gsl
        spdlog
        cxxopts

        # See FindLevelDB.cmake
        ${LEVELDB_LIBRARY}
        ${SNAPPY_LIBRARY}

        # Targets from config mode find_package
        gRPC::grpc
        gRPC::grpc++
        gRPC::grpc++_reflection
        protobuf::libprotobuf
        re2::re2
        )
