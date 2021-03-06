#!/usr/bin/python
import rospy
from digi.xbee.devices import ZigBeeDevice
from digi.xbee.models.mode import APIOutputMode
import struct
from digi.xbee.exception import InvalidPacketException, InvalidOperatingModeException, TimeoutException, XBeeException

def str2bool(v):
  return v.lower() in ("yes", "true", "t", "1")

class xbee_write():
    def __init__(self):
        rospy.init_node("xbee_write")
        PORT = rospy.get_param("~device","/dev/ttyUSB0")
        BAUD_RATE = rospy.get_param("~baud",115200)
        REMOTE_NODE_ID = rospy.get_param("~destination","REMOTE")
        self.device = ZigBeeDevice(PORT, BAUD_RATE)
        self.SOURCE_ENDPOINT = 0xA0
        self.DESTINATION_ENDPOINT = 0xA1
        self.CLUSTER_ID = 0x1554
        self.PROFILE_ID = 0x1234
        try:
            self.device.open()
            xbee_network = self.device.get_network()
            self.remote_device = xbee_network.discover_device(REMOTE_NODE_ID)
            if self.remote_device is None:
                rospy.logerr("Could not find the remote local_xbee: {}".format(REMOTE_NODE_ID))
                self.device.close()
            self.run()
        except Exception as exc:
            rospy.logerr(exc)

    def run(self):
        formatspec = 'cc?'
        while not rospy.is_shutdown():
            try:
                cmd = raw_input("Input commands, press Ctrl+D (linux) or Ctrl+Z+return (Windows) to exit, press h for list of commands:")
                if cmd=='h':
                    print("st[X]: start mission X=1, stop mission X=0\nrt[X]: Return home X=1, Resume Mission X=0")
                    continue
                if len(cmd)==3:
                    cmd = (cmd[0],cmd[1],str2bool(cmd[2]))
                data = struct.pack(formatspec,*cmd)
                rospy.loginfo("Sending explicit data to {} >> {}".format(self.remote_device.get_64bit_addr(),data))
                self.device.send_expl_data(self.remote_device,data,self.SOURCE_ENDPOINT,self.DESTINATION_ENDPOINT,self.CLUSTER_ID,self.PROFILE_ID)
            except EOFError:
                rospy.loginfo("Exiting")
                rospy.signal_shutdown("User Exit")
            except struct.error:
                rospy.logerr("Bad command, must contain 2 characters and a binary")
        self.device.close()

if __name__=="__main__":
    xbee_write()