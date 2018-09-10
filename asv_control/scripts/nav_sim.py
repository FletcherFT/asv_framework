#!/usr/bin/env python
# license removed for brevity
import rospy
from nav_msgs.msg import Odometry, Path
from sensor_msgs.msg import Imu, NavSatFix
from geometry_msgs.msg import *
from math import cos, sin
import tf
from simulation import fossen
from numpy import *
inv = linalg.inv
from pygeodesy import utm

class NavSimNode():
    def __init__(self):
        # let's make this a 3DoF simulator to begin with.
        # the goal is to work in odom and simulate the dynamics of a 3DoF vehicle (X,Y,Yaw), and publish noisy GPS and IMU measurements from them.
        rospy.init_node("simulator")

        # set the acceleration and velocity state vectors to 0
        self._a = zeros((3,1))
        self._v = zeros((3,1))

        # Initial GPS and IMU measurements
        self.Theta_en = rospy.get_param('~startingGPS',[-41.400419,147.125098])
        self.Theta_nb = rospy.get_param('~startingIMU',[0.0,pi/4,0.0])

        self.tfListener = tf.TransformListener()
        
        # Convert the GPS position into odom coordinates from utm frame
        self._utm = utm.toUtm(self.Theta_en[0],self.Theta_en[1])
        utmpoint = PointStamped()
        utmpoint.header.frame_id="utm"
        utmpoint.header.stamp = rospy.Time.now()
        utmpoint.point.x = self._utm._easting
        utmpoint.point.y = self._utm._northing
        utmpoint.point.z = 0.0
        odom_p = self._pointToOdom(utmpoint)
        if odom_p==-1:
            return

        self._eta_odom = array([[odom_p.point.x],[odom_p.point.y],[self.Theta_nb[2]]])

        self._m = rospy.get_param('~mass',15.0)
        self._xg = rospy.get_param('~xg',-0.1)
        self._Iz = rospy.get_param('~Iz',4.0)
        self._Xu = rospy.get_param('~Xu',-2.1)         # drag coefficient
        self._Xudot = rospy.get_param('~Xudot',-0.2)   # added mass coefficient (x dir)
        self._Yv = rospy.get_param('~Yv',-10.2)
        self._Yvdot = rospy.get_param('~Yvdot',-1.0)
        self._Yr = rospy.get_param('~Yr',0.15)
        self._Yrdot = rospy.get_param('~Yrdot',0.001)
        self._Nv = rospy.get_param('~Nv',-0.3)
        self._Nr = rospy.get_param('~Nr',-25)
        self._Nrdot = rospy.get_param('~Nrdot',-5)

        # MASS TERMS
        #inertial matrix Mrb = reshape([m,0,0,0,m,m*xg,0,m*xg,Iz])
        self.M_rb = rospy.get_param("~inertia",[self._m,0,0,0,self._m,self._m*self._xg,0,self._m*self._xg,self._Iz])
        if len(self.M_rb) == 3:
            self.M_rb = diag(self.M_rb)
        elif len(self.M_rb)==9:
            self.M_rb = reshape(self.M_rb,(3,3))
        else:
            rospy.logerr("DoF Error: Inertia matrix has a bad length, must be of length 3 or 9.")
            return
        #added mass matrix Ma = reshape([-Xudot,0,0,0,-Yvdot,-Yrdot,0,-Yrdot,-Nrdot])
        self.M_a = rospy.get_param("~added_mass",[-self._Xudot,0,0,0,-self._Yvdot,-self._Yrdot,0,-self._Yrdot,-self._Nrdot])
        if len(self.M_a) == 3:
            self.M_a = diag(self.M_a)
        elif len(self.M_a)==9:
            self.M_a = reshape(self.M_a,(3,3))
        else:
            rospy.logerr("DoF Error: Added mass matrix has a bad length, must be of length 3 or 9.")
            return
        # the mass matrix is the sum of the above two parameters
        self.M = self.M_rb+self.M_a

        #Initialise coriolis and centripetal matrix due to inertia (velocity is zero)
        self.C_rb = zeros((3,3))
        #Initialise coriolis and centripetal matrix due to added mass (this is a function of the added mass matrix and vel)
        self.C_a = zeros((3,3))

        #total damping matrix (potential + skin + wave damping coefficients)
        self.D = rospy.get_param("~damping",[-self._Xu,0,0,0,-self._Yv,-self._Yr,0,-self._Nv,-self._Nr])
        if len(self.D) == 3:
            self.D = diag(self.D)
        elif len(self.D) == 9:
            self.D = reshape(self.D,(3,3))
        else:
            rospy.logerr("DoF Error: Daming matrix has a bad length, must be of length 3 or 9.")
            return

        #external force terms
        self.tau_wind = zeros((3,1))
        self.tau_wave = zeros((3,1))

        #thruster control forces
        self.tau_sol = zeros((3,1))

        frequency = rospy.get_param('~frequency',100.0)
        rospy.Timer(rospy.Duration.from_sec(1.0/frequency),self.kinematics)
        rospy.Timer(rospy.Duration.from_sec(1.0/5.0),self.pub_gps)
        rospy.Timer(rospy.Duration.from_sec(1.0/30.0),self.pub_imu)

        rospy.Subscriber('tau_sol',WrenchStamped,self.wrenchCallback)

        self._gps = NavSatFix()
        self._gps.status.status = 0
        self._gps.status.service = 1
        self._gps.latitude = self.Theta_en[0]
        self._gps.longitude = self.Theta_en[1]
        self._gps.altitude = 0.0
        self._gps.position_covariance=[1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9]
        self._gps.position_covariance_type=3

        self._imu = Imu()

        self.gps_pub = rospy.Publisher('gps/fix',NavSatFix,queue_size=10)
        self.imu_pub = rospy.Publisher('imu/data',Imu,queue_size=10)
        
        rospy.spin()

    def _poseToUtm(self,pose):
        try:
            return self.tfListener.transformPose('utm',pose)
        except Exception as exc:
            rospy.logerr(exc)
            return -1

    def _pointToOdom(self,point):
        self.tfListener.waitForTransform("odom", "utm", rospy.Time(), rospy.Duration(4.0))
        try:
            return self.tfListener.transformPoint('odom',point)
        except Exception as exc:
            rospy.logerr(exc)
            return -1

    def wrenchCallback(self,wrench):
        #calculate acceleration from model
        self.tau_sol[0] = wrench.wrench.force.x
        self.tau_sol[1] = wrench.wrench.force.y
        self.tau_sol[2] = wrench.wrench.torque.z     

    def kinematics(self,event):
        #combine velocity and acceleration terms
        if not event.last_real is None:
            dt = (rospy.Time.now()-event.last_real).to_sec()
        else: 
            dt=0.0
        #update the coriolis matrix from the old velocity
        self.C_rb = fossen.updateCrb(self._v,self._m,self._xg)
        #update the added mas matrix from the old velocity
        self.C_a = fossen.updateCa(self._v,self._Xudot,self._Yvdot,self._Yrdot)

        N = self.C_a + self.C_rb + self.D      

        #calculate the fixed acceleration (based off old velocity and position)
        self._a = matmul(inv(self.M),self.tau_sol+self.tau_wind+self.tau_wave-matmul(N,self._v))
        # for idx,i in enumerate(self._a):
        #     if abs(i)<1e-5:
        #         self._a[idx]=0

        #update the velocity vector (body fixed) from new acceleration
        self._v += self._a*dt
        # for idx,i in enumerate(self._v):
        #     if abs(i)<1e-5:
        #         self._v[idx]=0

        #calculate the new orientation
        self._eta_odom[2] += self._v[2]*dt

        #obtain the rotation matrix by rotating through the euler angles
        R=tf.transformations.euler_matrix(0.0,0.0,self._eta_odom[2],'szyx')

        #update the position
        self._eta_odom[0:2]=self._eta_odom[0:2]+(matmul(R[0:2,0:2],self._v[0:2]))*dt

        # convert to utm
        pose = PoseStamped()
        pose.header.frame_id="odom"
        pose.header.stamp = rospy.Time.now()
        pose.pose.position.x=self._eta_odom[0]
        pose.pose.position.y=self._eta_odom[1]
        pose.pose.orientation=Quaternion(*tf.transformations.quaternion_from_euler(0,0,self._eta_odom[2],'sxyz'))
        pose = self._poseToUtm(pose)
        if pose == -1:
            return

        # send to GPS
        utmstr = "{} {} {} {}".format(self._utm.zone,self._utm.hemisphere,pose.pose.position.x,pose.pose.position.y)
        utmpoint = utm.parseUTM(utmstr)
        latlong = utmpoint.toLatLon()
        self._gps.status.status = 0
        self._gps.status.service = 1
        self._gps.latitude = latlong[0]
        self._gps.longitude = latlong[1]
        self._gps.altitude = 0.0
        self._gps.position_covariance=[1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9]
        self._gps.position_covariance_type=3

        # try:
        #     pose = self.tfListener.transformQuaternion('imu_link',pose)
        # except Exception as exc:
        #     rospy.logerr(exc)
        #     return
        self._imu.orientation = pose.pose.orientation
        self._imu.orientation_covariance = [1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9]
        self._imu.angular_velocity = Vector3(0,0,self._v[2])
        self._imu.angular_velocity_covariance = [1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9]
        self._imu.linear_acceleration = Vector3(*self._a)
        self._imu.linear_acceleration_covariance = [1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9,1e-9]
        
        rospy.logdebug("|\t{}\t|\t{}\t|\t{}\t|\t{}\t|\t{}\t|\t{}\t|".format(*vstack((self._a,self._v))))

    def pub_gps(self,event):
        self._gps.header.frame_id="gps_link"
        self._gps.header.stamp = rospy.Time.now()
        self.gps_pub.publish(self._gps)

    def pub_imu(self,event):
        self._imu.header.frame_id='imu_link'
        self._imu.header.stamp = rospy.Time.now()
        self.imu_pub.publish(self._imu)

if __name__ == '__main__':
    try:
        NavSimNode()
    except rospy.ROSInterruptException:
        pass
