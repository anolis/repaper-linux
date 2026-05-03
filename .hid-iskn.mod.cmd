cmd_/home/anolis/repaper_drv/hid-iskn.mod := printf '%s\n'   hid-iskn.o | awk '!x[$$0]++ { print("/home/anolis/repaper_drv/"$$0) }' > /home/anolis/repaper_drv/hid-iskn.mod
