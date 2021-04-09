cmake_minimum_required(VERSION 3.12)
project(distlock)
set(CMAKE_CXX_STANDARD 14)
set(CMAKE_VERBOSE_MAKEFILE ON)
add_definitions(-DGSL_THROW_ON_CONTRACT_VIOLATION)

set(CMAKE_CXX_FLAGS "-Wall -Wextra")
set(CMAKE_CXX_FLAGS_DEBUG "-g")
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -Wno-unused")


# Run protoc manually before running cmake.
file(GLOB GENERATED_PROTOBUF_FILES ${CMAKE_SOURCE_DIR}/generated/proto/*.pb.cc ${CMAKE_SOURCE_DIR}/generated/proto/*.pb.h)
set_source_files_properties(${GENERATED_PROTOBUF_FILES} PROPERTIES GENERATED TRUE)  # this doesn't seem to help for some reason
message(STATUS "Found generated protobuf files: ${GENERATED_PROTOBUF_FILES}")
add_library(distjob_proto STATIC ${GENERATED_PROTOBUF_FILES})
target_include_directories(distjob_proto PUBLIC
        ${PROTOBUF_INCLUDE_DIRS}
        ${CMAKE_SOURCE_DIR}/generated
        )
target_link_libraries(distjob_proto PUBLIC
        third_party
        )

add_subdirectory(third_party)

add_executable(distlock distlock.cc ${GENERATED_PROTOBUF_FILES})
target_link_libraries(distlock PUBLIC
        third_party
        distjob_proto
        -static
        -static-libgcc -static-libstdc++
        )
