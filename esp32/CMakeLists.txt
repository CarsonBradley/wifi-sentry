# wifi-sentry ESP32 CSI node. Build with ESP-IDF v6.1.x:
#   idf.py set-target esp32s3   (or esp32c6, esp32, esp32c3, esp32c5)
#   idf.py menuconfig           (wifi-sentry settings)
#   idf.py build flash monitor
cmake_minimum_required(VERSION 3.22)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(wifi_sentry_csi)
