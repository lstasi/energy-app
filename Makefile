install:
	mkdir -p /opt/energy-app
	cp src/server.py /opt/energy-app/
	cp -R src/static /opt/energy-app/
	cp systemd/energy-app.service /etc/systemd/system/
	systemctl enable energy-app.service
	systemctl start energy-app.service

uninstall:
	systemctl stop energy-app.service
	systemctl disable energy-app.service
	rm -rf /opt/energy-app
	rm /etc/systemd/system/energy-app.service
