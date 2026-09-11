cmake_minimum_required(VERSION 3.16)
project(seda_bus_compare_bench_cpp LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(SEDA_BUS_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(SEDA_BUS_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../../../seda-bus-cpp ${CMAKE_CURRENT_BINARY_DIR}/seda-bus-cpp)

add_executable(bench main.cpp)
target_link_libraries(bench PRIVATE seda_bus)
find_package(Threads REQUIRED)
target_link_libraries(bench PRIVATE Threads::Threads)
