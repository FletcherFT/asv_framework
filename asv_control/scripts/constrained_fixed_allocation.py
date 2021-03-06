#!/usr/bin/python
import rospy
from geometry_msgs.msg import WrenchStamped
from sensor_msgs.msg import JointState
from numpy import vstack, hstack, shape, diag, array, reshape, eye, zeros, ones, block, matmul
import numpy as np
from scipy.linalg import block_diag
from math import cos, sin, pi, radians, degrees
from asv_messages.msg import Thrusters
from asv_messages.srv import ConfigureSteppers
import quadprog

def quadprog_solve_qp(P, q, G=None, h=None, A=None, b=None):
    qp_G = .5 * (P + P.T)   # make sure P is symmetric
    qp_a = -q
    if A is not None:
        qp_C = -vstack([A, G]).T
        qp_b = -hstack([b, h])
        meq = A.shape[0]
    else:  # no equality constraint
        qp_C = -G.T
        qp_b = -h
        meq = 0
    return quadprog.solve_qp(qp_G, qp_a, qp_C, qp_b, meq)[0]

class ConstrainedNonrotatableAllocation:
    def __init__(self):
        rospy.init_node("allocator")
        # default is AP mode
        self.qpsetup(True)
        self.thrust_msg = Thrusters()
        self.thrust_pub = rospy.Publisher("thrusters",Thrusters,queue_size=10)
        self.sol_msg = WrenchStamped()
        self.sol_pub = rospy.Publisher("tau_sol",WrenchStamped,queue_size=10)
        self.mode_server = rospy.Service('~modeconfig',ConfigureSteppers,self.mode)
        self.motor_config = rospy.get_param('motorConfig')
        self.prev_pwms = np.array([0,0,0])
        rospy.Subscriber("tau_com/out",WrenchStamped,self.wrenchCallback)
        rospy.spin()

    def qpsetup(self,mode):
        if mode:
            self.thruster = rospy.get_param('thrusterAP')
        else:
            self.thruster = rospy.get_param('thrusterDP')
        self.r = 3
        self.n = self.thruster['n']
        self.W = diag(self.thruster['W'])
        self.Q = diag(self.thruster['Q'])
        self.Phi = block_diag(self.W,self.Q)
        self.R = zeros((self.r+self.n,self.n+2*self.r))
        for i in range(self.n):
            alpha = self.thruster['alpha'][i]
            lx = self.thruster['lx'][i]
            ly = self.thruster['ly'][i]
            if i==0:
                self.T=array([cos(alpha),sin(alpha),lx*sin(alpha)-ly*cos(alpha)])
            else:
                self.T=hstack((self.T,np.array([cos(alpha),sin(alpha),lx*sin(alpha)-ly*cos(alpha)])))
        self.T=reshape(self.T,(self.n,self.r)).T
        self.A1 = hstack((self.T,-eye(self.n)))
        self.C1 = hstack((eye(self.n),zeros((self.n,2*self.r))))
        self.A2 = block([
            [-eye(self.r),zeros((self.r,self.n))],
            [eye(self.r),zeros((self.r,self.n))],
            [zeros((self.r,self.r)),zeros((self.r,self.n))],
            [zeros((self.r,self.r)),zeros((self.r,self.n))]
        ])
        self.C2 = block([
            [zeros((self.r,self.n)),-eye(self.r),zeros((self.r,self.r))],
            [zeros((self.r,self.n+self.r)),eye(self.r)],
            [zeros((2*self.r,self.n+self.r*2))]
        ])

    def wrenchCallback(self,msg):
        tau_com = array([msg.wrench.force.x,msg.wrench.force.y,msg.wrench.torque.z])
        p = hstack((tau_com,array(self.thruster['fmin']),array(self.thruster['fmax'])))
        P = self.Phi
        q = matmul(self.R,p)
        A = self.A1
        b = matmul(self.C1,p)
        G = self.A2
        h = matmul(self.C2,p)
        try:
            #solve for x = [df,da,s]
            z = quadprog_solve_qp(P,q,G,h,A,b)
        except ValueError as exc:
            rospy.logerr(exc)
        except Exception as exc:
            rospy.logerr(exc)
        finally:
            pass
        thrusts = z[0:self.n]
        slacks = z[self.n:2*self.n]
        s = "|"+(2*self.n)*"\t{:.4}\t|"
        rospy.logdebug(s.format(*z))
        
        self.thrust_msg.header.stamp = rospy.Time.now()
        self.thrust_msg.header.frame_id = str(self.thruster['name'])
        # compute pwms and check thrusts for deadband cutoff.
        self.thrust_msg.pwm,thrusts = self.forceToPWM(thrusts)
        self.thrust_msg.rpm = self.forceToRPM(thrusts)
        self.thrust_msg.force = thrusts
        self.thrust_pub.publish(self.thrust_msg)
        tau_sol = matmul(self.T,thrusts)
        self.sol_msg.header.stamp = rospy.Time.now()
        self.sol_msg.header.frame_id = "base_link"
        self.sol_msg.wrench.force.x = tau_sol[0]
        self.sol_msg.wrench.force.y = tau_sol[1]
        self.sol_msg.wrench.torque.z = tau_sol[2]
        self.sol_pub.publish(self.sol_msg)

    def mode(self,request):
        try:
            self.qpsetup(request.mode)
            if request.mode:
                S="Configuring for AP mode"
            else:
                S="Configuring for DP mode"
        except Exception as exc:
            response=[False,str(exc)]
        else:
            rospy.logdebug(S)
            response=[True,S]
        return response

    def forceToRPM(self,thrusts):
        rpm = [0,0,0]
        for idx,i in enumerate(thrusts):
            rpm[idx]=self.motor_config['Krpm'][idx]*abs(i)**2
            if i<0:
                rpm[idx]*=-1
        return rpm
    
    def forceToPWM(self,thrusts):
        pwm = np.array([0,0,0])
        thrusts = np.array(thrusts)
        K2 = np.array(self.motor_config['Order2Kpwm'])
        K1 = np.array(self.motor_config['Order1Kpwm'])
        K0 = np.array(self.motor_config['Order0Kpwm'])
        pwm = K2*abs(thrusts)**2 + K1*abs(thrusts) + K0
        deadband = np.array(self.motor_config['deadband'])
        pwm[pwm<deadband]=0
        pwm[thrusts<0]*=-1
        deltapwm = pwm-self.prev_pwms
        aggression = self.motor_config['aggression']
        for idx,i in enumerate(deltapwm):
            if abs(i)>aggression:
                if i<0:
                    pwm[idx]=self.prev_pwms[idx]-aggression
                else:
                    pwm[idx]=self.prev_pwms[idx]+aggression
        self.prev_pwms=pwm
        return pwm,thrusts


if __name__ == "__main__":
    try:
        node = ConstrainedNonrotatableAllocation()
    except KeyError as e:
        rospy.logerr("Parameters not found!")
    except rospy.ROSInterruptException:
        pass
