install:
	mkdir -p /home/pi/energy-app
	cp server.py /home/pi/energy-app/
	cp -R static /home/pi/energy-app/
	cp energy-app.service /etc/systemd/system/
	systemctl enable energy-app.service
	systemctl start energy-app.service

uninstall:
	systemctl stop energy-app.service
	systemctl disable energy-app.service
	rm -rf /home/pi/energy-app
	rm /etc/systemd/system/energy-app.service
