# remote-switch
Raspberry pi connected to a mobile/cell network to turn on/off an AC-powered device.

I use it in an airplane hanger to turn on an airplane oil pan heater, but it would work anywhere there is reception and power to turn on/off any device

## Equipment

Links provided for your convenience, but buy from whereever you prefer

* [Raspberry pi zero w](https://www.raspberrypi.com/products/raspberry-pi-zero-w/)
  * You'll need a microSD card if you don't have one. 4GB+ will be enough.
* [SIM7600 LTE modem HAT for pi](https://www.waveshare.com/sim7600a-h-4g-hat.htm) also available on [amazon](https://www.amazon.com/SIM7600A-H-4G-HAT-Communication-Positioning/dp/B082WH85WV/)
  * You'll need a sim card, the docs say it takes a nano but the unit I had a mini sim slot. I used a Google fi sim since it only costs data on my current existing plan
  * You could skip this if you have reliable wifi already in place
* [Digital Loggers IoT relay](https://dlidirect.com/products/iot-power-relay)
  * Connect the `-` to GND on the pi, and the `+` to an unused GPIO pin

## Configuatiuon

1. Get your pi set up, I used [raspbian lite](https://www.raspberrypi.com/software/operating-systems/) and there are many guides out there on the basics, here are the specifics for this project:
   - Pick a GPIO pin that is unused by the LTE hat, I used `17`. You'll need to update the below to yours if different
   - Add the below to `/boot/config.txt`:

      ```
      # https://forums.raspberrypi.com/viewtopic.php?f=117&t=208748
      # set gpio pin 17 as output and set to low
      gpio=17=op,dl
      ```
   - Add to /etc/rc.local:
   
      ```
      echo "17" > /sys/class/gpio/export
      ```

2. Get your LTE modem running, the [manufacturers documentation](https://www.waveshare.com/wiki/SIM7600A-H_4G_HAT) is detailed but not always clear, I used a bit of trial and error with things like APN. 

3. Install a webserver, I used `apache`
   - Add apache user `www-data` to the gpio group to read/write data  
    
      ```
      sudo usermod -a -G gpio www-data
      ```

4. Install a firewall, I used `ufw` and closed all ports except `ssh` and `http` 

5. (optional) I installed `openvpn` to connect to an existing private network

6. Edit `switch.sh` to have the correct `$gpio_pin` value for the GPIO pin you used for the switch control
   - copy to your cgi-bin
   - `chmod 0755 switch.sh`
  
 It should look something like this:

![Screenshot_20240122-141822](https://github.com/leithl/remote-switch/assets/109554/7746230c-8149-42dd-b0e1-d68cac6025ed)
