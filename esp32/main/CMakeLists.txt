idf_component_register(SRCS "main.c" "csi_encode.c" "ws_sign.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_wifi esp_netif esp_event esp_timer nvs_flash lwip mbedtls)
