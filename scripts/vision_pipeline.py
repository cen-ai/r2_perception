#!/usr/bin/env python2.7

# R2 Perception - Hanson Robotics Unified Perception System, v1.0
# by Desmond Germans

from __future__ import with_statement
import os
import rospy
import numpy
import time
import cv2
import tf
import math
import geometry_msgs
from dynamic_reconfigure.server import Server
from r2_perception.cfg import vision_pipelineConfig as VisionConfig
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
from math import sqrt
from r2_perception.msg import Face,Hand,Saliency,FaceRequest,FaceResponse,CandidateUser,CandidateHand,CandidateSaliency
from visualization_msgs.msg import Marker
from threading import Lock
from geometry_msgs.msg import Point,PointStamped


opencv_bridge = CvBridge()

# maximum fuse distance between faces (meters)
face_continuity_threshold_m = 0.5

# maximum fuse distance between hands (meters)
hand_continuity_threshold_m = 0.5

# maximum fuse distance between salient points (meters)
saliency_continuity_threshold_m = 0.3

# minimum confidence needed for an observation to be valid (0..1)
minimum_confidence = 0.4

# maximum extrapolation time (seconds)
time_difference = rospy.Time(1,0)

# number of points needed for full confidence
full_points = 5


serial_number = 0

def GenerateCandidateUserID():
    global serial_number
    result = serial_number
    serial_number += 10
    return result

def GenerateCandidateHandID():
    global serial_number
    result = serial_number
    serial_number += 10
    return result

def GenerateCandidateSaliencyID():
    global serial_number
    result = serial_number
    serial_number += 10
    return result


class VisionPipeline(object):


    # constructor
    def __init__(self):

        # create lock
        self.lock = Lock()

        # start possible debug window
        cv2.startWindowThread()

        # clear candidate dicts
        self.cusers = {}
        self.chands = {}
        self.csaliencies = {}

        # get pipeline name
        self.name = rospy.get_namespace().split('/')[-2]
        self.camera_id = hash(self.name) & 0xFFFFFFFF

        # get parameters
        self.session_tag = str(rospy.get_param("/session_tag"))
        self.session_id = hash(self.session_tag) & 0xFFFFFFFF

        self.debug_vision_flag = rospy.get_param("debug_vision_flag")
        if self.debug_vision_flag:
            cv2.namedWindow(self.name)
            self.frame_sub = rospy.Subscriber("camera/image_raw",Image,self.HandleFrame)

        self.visualize_flag = rospy.get_param("visualize_flag")
        self.visualize_candidates_flag = rospy.get_param("visualize_candidates_flag")
        if self.visualization_flag and self.visualize_candidates_flag:
            self.face_rviz_pub = rospy.Publisher("rviz_face",Marker,queue_size=5)
            self.hand_rviz_pub = rospy.Publisher("rviz_hand",Marker,queue_size=5)
            self.saliency_rviz_pub = rospy.Publisher("rviz_saliency",Marker,queue_size=5)

        self.thumbs_dir = rospy.get_param("thumbs_dir")
        self.thumbs_ext = rospy.get_param("thumbs_ext")
        self.store_thumbs_flag = rospy.get_param("store_thumbs_flag")
        if self.store_thumbs_flag:
            today_dir = time.strftime("%Y%m%d")
            camera_dir = self.name + "_%08X" % (self.camera_id & 0xFFFFFFFF)
            self.thumbs_output_dir = self.thumbs_dir + "/" + today_dir + "/" + self.session_tag + "_%08X/" & (self.session_id & 0xFFFFFFFF) + camera_tag + "/"
            if not os.path.exists(self.thumbs_output_dir):
                os.makedirs(self.thumbs_output_dir)

        self.fovy = rospy.get_param("fovy")
        self.aspect = rospy.get_param("aspect")
        self.rotate = rospy.get_param("rotate")

        self.vision_rate = rospy.get_param("vision_rate")
        self.timer = rospy.Timer(rospy.Duration(1.0 / self.vision_rate),self.HandleTimer)

        self.face_regression_flag = rospy.get_param("face_regression_flag")
        self.hand_regression_flag = rospy.get_param("hand_regression_flag")
        self.saliency_regression_flag = rospy.get_param("saliency_regression_flag")

        self.face_fuse_distance = rospy.get_param("face_fuse_distance")
        self.hand_fuse_distance = rospy.get_param("hand_fuse_distance")
        self.saliency_fuse_distance = rospy.get_param("saliency_fuse_distance")

        self.min_face_confidence = rospy.get_param("min_face_confidence")
        self.min_hand_confidence = rospy.get_param("min_hand_confidence")
        self.min_saliency_confidence = rospy.get_param("min_saliency_confidence")

        self.face_keep_time = rospy.get_param("face_keep_time")
        self.hand_keep_time = rospy.get_param("hand_keep_time")
        self.saliency_keep_time = rospy.get_param("saliency_keep_time")

        self.full_face_points = rospy.get_param("full_face_points")
        self.full_hand_points = rospy.get_param("full_hand_points")
        self.full_saliency_points = rospy.get_param("full_saliency_points")

        # start dynamic reconfigure server
        self.config_server = Server(VisionConfig,self.HandleConfig)

        # start listening to transforms
        self.listener = tf.TransformListener()

        # start remaining subscribers and publishers
        self.face_response_sub = rospy.Subscriber("face_response",FaceResponse,self.HandleFaceResponse)
        self.face_request_pub = rospy.Publisher("face_request",FaceRequest,queue_size=5)

        self.face_sub = rospy.Subscriber("raw_face",Face,self.HandleFace)
        self.hand_sub = rospy.Subscriber("raw_hand",Hand,self.HandleHand)
        self.saliency_sub = rospy.Subscriber("raw_saliency",Saliency,self.HandleSaliency)

        self.cuser_pub = rospy.Publisher("cuser",CandidateUser,queue_size=5)
        self.chand_pub = rospy.Publisher("chand",CandidateHand,queue_size=5)
        self.csaliency_pub = rospy.Publisher("csaliency",CandidateSaliency,queue_size=5)
 

    def HandleConfig(self,data,level):

        new_session_tag = data.session_tag
        if new_session_tag != self.session_tag:
        self.session_tag = new_session_tag
            self.session_id = hash(self.session_tag) & 0xFFFFFFFF

        new_debug_vision_flag = data.debug_vision_flag
        if new_debug_vision_flag != self.debug_vision_flag:
            self.debug_vision_flag = new_debug_vision_flag
            if self.debug_vision_flag:
                cv2.namedWindow(self.name)
                self.frame_sub = rospy.Subscriber("camera/image_raw",Image,self.HandleFrame)
            else:
                cv2.destroyWindow(self.name)
                self.frame_sub.unregister()

        new_visualize_flag = data.visualize_flag
        new_visualize_candidates_flag = data.visualize_candidates_flag
        if (new_visualize_flag != self.visualize_flag) or (new_visualize_candidates_flag != self.visualize_candidates_flag):
            self.visualize_flag = new_visualize_flag
            self.visualize_candidates_flag = new_visualize_candidates_flag
            if self.visualization_flag and self.visualize_candidates_flag:
                self.face_rviz_pub = rospy.Publisher("rviz_face",Marker,queue_size=5)
                self.hand_rviz_pub = rospy.Publisher("rviz_hand",Marker,queue_size=5)
                self.saliency_rviz_pub = rospy.Publisher("rviz_saliency",Marker,queue_size=5)
            else:
                self.face_rviz_pub.unregister()
                self.hand_rviz_pub.unregister()
                self.saliency_rviz_pub.unregister()

        new_thumbs_dir = data.thumbs_dir
        self.thumbs_ext = data.thumbs_ext
        new_store_thumbs_flag = data.store_thumbs_flag
        if (new_thumbs_dir != self.thumbs_dir) or (new_store_thumbs_flag != self.store_thumbs_flag):
            self.thumbs_dir = new_thumbs_dir
            self.store_thumbs_flag = new_store_thumbs_flag
            if self.store_thumbs_flag:
                today_dir = time.strftime("%Y%m%d")
                camera_dir = self.name + "_%08X" % (self.camera_id & 0xFFFFFFFF)
                self.thumbs_output_dir = self.thumbs_dir + "/" + today_dir + "/" + self.session_tag + "_%08X/" & (self.session_id & 0xFFFFFFFF) + camera_tag + "/"
                if not os.path.exists(self.thumbs_output_dir):
                    os.makedirs(self.thumbs_output_dir)

        self.fovy = data.fovy
        self.aspect = data.aspect
        self.rotate = data.rotate

        new_vision_rate = data.vision_rate
        if new_vision_rate != self.vision_rate:
            self.vision_rate = new_vision_rate
            self.timer.shutdown()
            self.timer = rospy.Timer(rospy.Duration(1.0 / self.vision_rate),self.HandleTimer)

        self.face_regression_flag = data.face_regression_flag
        self.hand_regression_flag = data.hand_regression_flag
        self.saliency_regression_flag = data.saliency_regression_flag

        self.face_fuse_distance = data.face_fuse_distance
        self.hand_fuse_distance = data.hand_fuse_distance
        self.saliency_fuse_distance = data.saliency_fuse_distance

        self.min_face_confidence = data.min_face_confidence
        self.min_hand_confidence = data.min_hand_confidence
        self.min_saliency_confidence = data.min_saliency_confidence

        self.face_keep_time = data.face_keep_time
        self.hand_keep_time = data.hand_keep_time
        self.saliency_keep_time = data.saliency_keep_time

        self.full_face_points = data.full_face_points
        self.full_hand_points = data.full_hand_points
        self.full_saliency_points = data.full_saliency_points

        return data


    # when a newly detected face arrives
    def HandleFace(self,data):

        with self.lock:

            # if timestamp is missing, generate one
            if data.ts.secs == 0:
                data.ts = rospy.get_rostime()

            # find closest candidate user
            closest_cuser_id = 0
            closest_dist = self.face_fuse_distance
            for cuser_id in self.cusers:

                # find where the candidate user would be right now
                if self.face_regression_flag:
                    face = self.cusers[cuser_id].Extrapolate(data.ts)
                else:
                    face = self.cusers[cuser_id].faces[-1]

                # calculate distance
                dx = data.position.x - face.position.x
                dy = data.position.y - face.position.y
                dz = data.position.z - face.position.z
                d = sqrt(dx * dx + dy * dy + dz * dz)

                # update closest
                if (closest_cuser_id == 0) or (d < closest_dist):
                    closest_cuser_id = cuser_id
                    closest_dist = d

            # if close enough to existing face
            if closest_dist < face_fuse_distance:

                # fuse with existing candidate user
                self.cusers[closest_cuser_id].Append(data)

            else:

                # create new candidate user, starting with this face
                closest_cuser_id = GenerateCandidateUserID()
                cuser = UserPredictor()
                cuser.Append(data)

                self.cusers[closest_cuser_id] = cuser

                # send face analysis request to face_analysis
                msg = FaceRequest()
                msg.session_id = self.session_id
                msg.camera_id = self.camera_id
                msg.cuser_id = closest_cuser_id
                msg.face_id = data.face_id
                msg.ts = data.ts
                msg.thumb = data.thumb
                self.face_request_pub.publish(msg)

            # store thumbnail
            if self.store_thumbs_flag:

                cuser_tag = "cuser_%08X" % (closest_cuser_id & 0xFFFFFFFF)
                thumb_dir = self.thumbs_output_dir + cuser_tag + "/"
                if not os.path.exists(thumb_dir):
                    os.makedirs(thumb_dir)
                image = opencv_bridge.imgmsg_to_cv2(data.thumb)
                face_tag = "face_%08X" % (data.face_id & 0xFFFFFFFF)
                path = thumb_dir + "/" + face_tag + self.thumbs_ext
                cv2.imwrite(path,image)


    # when a newly detected hand arrives
    def HandleHand(self,data):

        with self.lock:

            # if timestamp is missing, generate one
            if data.ts.secs == 0:
                data.ts = rospy.get_rostime()

            # find closest candidate user
            closest_chand_id = 0
            closest_dist = self.hand_fuse_distance
            for chand_id in self.chands:

                # find where the candidate user would be right now
                if self.hand_regression_flag:
                    hand = self.chands[chand_id].Extrapolate(data.ts)
                else:
                    hand = self.chands[chand_id].hands[-1]

                # calculate distance
                dx = data.position.x - hand.position.x
                dy = data.position.y - hand.position.y
                dz = data.position.z - hand.position.z
                d = sqrt(dx * dx + dy * dy + dz * dz)

                # update closest                
                if (closest_chand_id == 0) or (d < closest_dist):
                    closest_chand_id = chand_id
                    closest_dist = d

            # if close enough to existing hand
            if closest_dist < self.hand_fuse_distance:

                # fuse with existing candidate hand
                self.chands[closest_chand_id].Append(data)

            else:

                # create new candidate hand
                closest_chand_id = GenerateCandidateHandID()
                chand = HandPredictor()
                chand.Append(data)

                self.chands[closest_chand_id] = chand


    # when a newly detected saliency vector arrives
    def HandleSaliency(self,data):

        with self.lock:

            # if timestamp is missing, generate one
            if data.ts.secs == 0:
                data.ts = rospy.get_rostime()

            # find closest candidate saliency vector
            closest_csaliency_id = 0
            closest_dist = self.saliency_fuse_distance
            for csaliency_id in self.csaliencies:

                # find where the candidate user would be right now                
                if self.saliency_regression_flag:
                    saliency = self.csaliencies[csaliency_id].Extrapolate(data.ts)
                else:
                    saliency = self.csaliencies[csaliency_id].saliencies[-1]

                # calculate distance
                dx = data.direction.x - saliency.direction.x
                dy = data.direction.y - saliency.direction.y
                dz = data.direction.z - saliency.direction.z
                d = sqrt(dx * dx + dy * dy + dz * dz)

                # update closest                
                if (closest_csaliency_id == 0) or (d < closest_dist):
                    closest_csaliency_id = csaliency_id
                    closest_dist = d

            # if close enough to existing saliency vector
            if closest_dist < self.saliency_fuse_distance

                # fuse with existing candidate saliency vector
                self.csaliencies[closest_csaliency_id].Append(data)

            else:

                # create new candidate saliency vector
                closest_csaliency_id = GenerateCandidateSaliencyID()
                csaliency = SaliencyPredictor()
                csaliency.Append(data)

                self.csaliencies[closest_csaliency_id] = csaliency


    # when a face was analyzed
    def HandleFaceResponse(self,data):

        # if the candidate user doesn't exist anymore, forget the result
        if data.cuser_id not in self.cusers:
            return

        with self.lock:

            # update the settings
            self.cusers[data.cuser_id].age = data.age
            self.cusers[data.cuser_id].age_confidence = data.age_confidence
            self.cusers[data.cuser_id].gender = data.gender
            self.cusers[data.cuser_id].gender_confidence = data.gender_confidence
            self.cusers[data.cuser_id].identity = data.identity
            self.cusers[data.cuser_id].identity_confidence = data.identity_confidence


    # when a new camera image arrives
    def HandleFrame(self,data):

        # calculate distance to camera plane
        cpd = 1.0 / math.tan(self.fovy)

        with self.lock:

            # convert image from ROS to OpenCV and unrotate
            image = opencv_bridge.imgmsg_to_cv2(data,"bgr8")
            width = image.shape[1]
            height = image.shape[0]
            if self.rotate == 90:
                width = image.shape[0]
                height = image.shape[1]
                image = cv2.transpose(image)
            elif self.rotate == -90:
                width = image.shape[0]
                height = image.shape[1]
                image = cv2.transpose(image)
                image = cv2.flip(image,1)
            elif self.rotate == 180:
                image = cv2.flip(image,-1)
                image = cv2.flip(image,1)

            # get current time
            ts = rospy.get_rostime()

            # display candidate users as red circles
            for cuser_id in self.cusers:

                # the face
                if self.face_regression_flag:
                    face = self.cusers[cuser_id].Extrapolate(ts)
                else:
                    face = self.cusers[cuser_id].faces[-1]
                x = int((0.5 + 0.5 * face.rect.origin.x) * float(width))
                y = int((0.5 + 0.5 * face.rect.origin.y) * float(height))
                cv2.circle(image,(x,y),10,(0,0,255),2)

                # annotate with info if available
                label = ""
                if self.cusers[cuser_id].identity_confidence > 0.0:
                    label = self.cusers[cuser_id].identity
                if self.cusers[cuser_id].age_confidence > 0.0:
                    if label != "":
                        label += ", "
                    label += str(int(self.cusers[cuser_id].age)) + " y/o"
                if self.cusers[cuser_id].gender_confidence > 0.0:
                    if label != "":
                        label += ", "
                    if self.cusers[cuser_id].gender == 2:
                        label += "female"
                    else:
                        label += "male"
                cv2.putText(image,label,(x - 20,y + 20),cv2.cv.CV_FONT_HERSHEY_PLAIN,1,(0,255,255))

            # TODO: display hands as green circles

            # display saliency vectors as blue circles
            for csaliency_id in self.csaliencies:

                if self.saliency_regression_flag:
                    saliency = self.csaliencies[csaliency_id].Extrapolate(ts)
                else:
                    saliency = self.csaliencies[csaliency_id].saliencies[-1]

                # convert vector back to 2D camera position for visualization
                fx = 1.0
                fy = saliency.direction.y / saliency.direction.x
                fz = saliency.direction.z / saliency.direction.x
                px = int(0.5 * (1.0 - fy * cpd) * float(width))
                py = int(0.5 * (1.0 - fz * cpd) * float(height))
                cv2.circle(image,(px,py),10,(255,0,0),2)

            cv2.imshow(self.name,image)


    # send markers to RViz for a user
    def SendUserMarkers(self,frame_id,ts,cuser_id,ns,position):

        # the face
        marker = Marker()
        marker.header.frame_id = frame_id
        marker.header.stamp = ts
        marker.id = cuser_id
        marker.ns = ns
        marker.type = Marker.SPHERE
        marker.action = Marker.MODIFY
        marker.pose.position.x = position.x
        marker.pose.position.y = position.y
        marker.pose.position.z = position.z
        marker.pose.orientation.x = 0.0
        marker.pose.orientation.y = 0.0
        marker.pose.orientation.z = 0.0
        marker.pose.orientation.w = 1.0
        marker.scale.x = 0.1
        marker.scale.y = 0.1
        marker.scale.z = 0.1
        marker.color.r = 1.0
        marker.color.g = 0.0
        marker.color.b = 0.0
        marker.color.a = 0.5
        marker.lifetime = rospy.Duration(2,0)
        marker.frame_locked = False
        self.face_rviz_pub.publish(marker)

        # label under the face
        marker = Marker()
        marker.header.frame_id = frame_id
        marker.header.stamp = ts
        marker.id = cuser_id + 1
        marker.ns = ns
        marker.type = Marker.TEXT_VIEW_FACING
        marker.action = Marker.MODIFY
        marker.pose.position.x = position.x
        marker.pose.position.y = position.y
        marker.pose.position.z = position.z - 0.1
        marker.pose.orientation.x = 0.0
        marker.pose.orientation.y = 0.0
        marker.pose.orientation.z = 0.0
        marker.pose.orientation.w = 1.0
        marker.scale.x = 0.05
        marker.scale.y = 0.05
        marker.scale.z = 0.05
        marker.color.r = 0.7
        marker.color.g = 0.7
        marker.color.b = 0.7
        marker.color.a = 0.5
        if self.cusers[cuser_id].gender == 2:
            gender = "female"
        else:
            gender = "male"
        marker.text = self.name + " candidate {} ({} {})".format(cuser_id,gender,int(self.cusers[cuser_id].age))
        marker.lifetime = rospy.Duration(2,0)
        marker.frame_locked = False
        self.face_rviz_pub.publish(marker)


    # send markers to RViz for a hand
    def SendHandMarkers(self,frame_id,ts,chand_id,ns,position,gestures):

        # the hand
        marker = Marker()
        marker.header.frame_id = frame_id
        marker.header.stamp = ts
        marker.id = chand_id
        marker.ns = ns
        marker.type = Marker.SPHERE
        marker.action = Marker.MODIFY
        marker.pose.position.x = position.x
        marker.pose.position.y = position.y
        marker.pose.position.z = position.z
        marker.pose.orientation.x = 0.0
        marker.pose.orientation.y = 0.0
        marker.pose.orientation.z = 0.0
        marker.pose.orientation.w = 1.0
        marker.scale.x = 0.1
        marker.scale.y = 0.1
        marker.scale.z = 0.1
        marker.color.r = 0.0
        marker.color.g = 1.0
        marker.color.b = 0.0
        marker.color.a = 0.5
        marker.lifetime = rospy.Duration(2,0)
        marker.frame_locked = False
        self.hand_rviz_pub.publish(marker)

        # label under the hand
        marker = Marker()
        marker.header.frame_id = frame_id
        marker.header.stamp = ts
        marker.id = chand_id + 1
        marker.ns = ns
        marker.type = Marker.TEXT_VIEW_FACING
        marker.action = Marker.MODIFY
        marker.pose.position.x = position.x
        marker.pose.position.y = position.y
        marker.pose.position.z = position.z - 0.1
        marker.pose.orientation.x = 0.0
        marker.pose.orientation.y = 0.0
        marker.pose.orientation.z = 0.0
        marker.pose.orientation.w = 1.0
        marker.scale.x = 0.05
        marker.scale.y = 0.05
        marker.scale.z = 0.05
        marker.color.r = 0.7
        marker.color.g = 0.7
        marker.color.b = 0.7
        marker.color.a = 0.5
        marker.text = self.name + " hand: "
        for gesture in gestures:
            marker.text += " " + gesture
        marker.lifetime = rospy.Duration(2,0)
        marker.frame_locked = False
        self.hand_rviz_pub.publish(marker)


    # send markers to RViz for a saliency vector
    def SendSaliencyMarker(self,frame_id,ts,csaliency_id,ns,direction):

        # the arrow
        marker = Marker()
        marker.header.frame_id = frame_id
        marker.header.stamp = ts
        marker.id = csaliency_id
        marker.ns = ns
        marker.type = Marker.ARROW
        marker.action = Marker.MODIFY
        marker.pose.position.x = 0
        marker.pose.position.y = 0
        marker.pose.position.z = 0
        marker.pose.orientation.x = 0.0
        marker.pose.orientation.y = 0.0
        marker.pose.orientation.z = 0.0
        marker.pose.orientation.w = 1.0
        marker.scale.x = 0.01
        marker.scale.y = 0.03
        marker.scale.z = 0.08
        marker.color.r = 0.0
        marker.color.g = 0.0
        marker.color.b = 1.0
        marker.color.a = 0.5
        points = []
        points.append(Point(0.0,0.0,0.0))
        points.append(Point(direction.x,direction.y,direction.z))
        marker.points = points
        marker.lifetime = rospy.Duration.from_sec(0.1)
        marker.frame_locked = False
        self.saliency_rviz_pub.publish(marker)


    # at vision rate
    def HandleTimer(self,data):

        # get current timestamp
        ts = data.current_expected

        with self.lock:

            # mine the current candidate users for confident ones and send them off
            for cuser_id in self.cusers:
                conf = self.cusers[cuser_id].CalculateConfidence(self.full_face_points)
                if conf >= self.min_face_confidence:

                    # get best candidate user
                    if self.face_regression_flag:
                        cuser = self.cusers[cuser_id].Extrapolate(ts)
                    else:
                        cuser = self.cusers[cuser_id].faces[-1]

                    if self.listener.canTransform("world",self.name,ts):

                        # make a PointStamped structure to satisfy TF
                        ps = PointStamped()
                        ps.header.seq = 0
                        ps.header.stamp = ts
                        ps.header.frame_id = self.name
                        ps.point.x = cuser.position.x
                        ps.point.y = cuser.position.y
                        ps.point.z = cuser.position.z

                        # transform to world coordinates
                        pst = self.listener.transformPoint("world",ps)

                        # setup candidate user message
                        msg = CandidateUser()
                        msg.session_id = self.session_id
                        msg.camera_id = self.camera_id
                        msg.cuser_id = cuser_id
                        msg.ts = ts
                        msg.position.x = pst.point.x
                        msg.position.y = pst.point.y
                        msg.position.z = pst.point.z
                        msg.confidence = cuser.confidence
                        msg.smile = cuser.smile
                        msg.frown = cuser.frown
                        msg.expressions = cuser.expressions
                        msg.age = self.cusers[cuser_id].age
                        msg.age_confidence = self.cusers[cuser_id].age_confidence
                        msg.gender = self.cusers[cuser_id].gender
                        msg.gender_confidence = self.cusers[cuser_id].gender_confidence
                        msg.identity = self.cusers[cuser_id].identity
                        msg.identity_confidence = self.cusers[cuser_id].identity_confidence
                        self.cuser_pub.publish(msg)

                    # output markers to rviz
                    if self.visualization_flag and self.visualize_candidates_flag:
                        self.SendUserMarkers(self.name,ts,cuser_id,"/robot/perception/{}".format(self.name),cuser.position)

            # prune the candidate users and remove them if they disappeared
            to_be_removed = []
            prune_before_time = ts - self.face_keep_time
            for cuser_id in self.cusers:
                self.cusers[cuser_id].PruneBefore(prune_before_time)
                if len(self.cusers[cuser_id].faces) == 0:
                    to_be_removed.append(cuser_id)
            for key in to_be_removed:
                del self.cusers[key]

            # mine the current candidate hands for confident ones and send them off
            for chand_id in self.chands:
                conf = self.chands[chand_id].CalculateConfidence(self.full_hand_points)
                if conf >= self.min_hand_confidence:

                    # get best candidate hand
                    if self.hand_regression_flag:
                        chand = self.chands[chand_id].Extrapolate(ts)
                    else:
                        chand = self.chands[chand_id].hands[-1]

                    if self.listener.canTransform("world",self.name,ts):

                        # make a PointStamped structure to satisfy TF
                        ps = PointStamped()
                        ps.header.seq = 0
                        ps.header.stamp = ts
                        ps.header.frame_id = self.name
                        ps.point.x = chand.position.x
                        ps.point.y = chand.position.y
                        ps.point.z = chand.position.z

                        # transform to world coordinates
                        pst = self.listener.transformPoint("world",ps)

                        # setup candidate hand message
                        msg = CandidateHand()
                        msg.session_id = self.session_id
                        msg.camera_id = self.camera_id
                        msg.chand_id = chand_id
                        msg.ts = ts
                        msg.position.x = pst.point.x
                        msg.position.y = pst.point.y
                        msg.position.z = pst.point.z
                        msg.confidence = chand.confidence
                        self.chand_pub.publish(msg)

                    # output markers to rviz
                    if self.visualization_flag and self.visualize_candidates_flag:
                        self.SendHandMarkers(self.name,ts,chand_id,"/robot/perception/{}".format(self.name),chand.position,chand.gestures)

            # prune the candidate hands and remove them if they disappeared
            to_be_removed = []
            prune_before_time = ts - self.hand_keep_time
            for chand_id in self.chands:
                self.chands[chand_id].PruneBefore(prune_before_time)
                if len(self.chands[chand_id].hands) == 0:
                    to_be_removed.append(chand_id)
            for key in to_be_removed:
                del self.chands[key]


            # mine the current candidate saliencies for confident ones and send them off
            for csaliency_id in self.csaliencies:
                conf = self.csaliencies[csaliency_id].CalculateConfidence(self.full_saliency_points)
                if conf >= self.min_saliency_confidence:

                    # get best candidate saliency
                    if self.saliency_regression_flag:
                        csaliency = self.csaliencies[csaliency_id].Extrapolate(ts)
                    else:
                        csaliency = self.csaliencies[csaliency_id].saliencies[-1]

                    msg = CandidateSaliency()
                    msg.session_id = self.session_id
                    msg.camera_id = self.camera_id
                    msg.csaliency_id = csaliency_id
                    msg.ts = ts
                    msg.direction = csaliency.direction
                    msg.confidence = csaliency.confidence
                    self.csaliency_pub.publish(msg)

                    # output markers to rviz
                    if self.visualization_flag and self.visualize_candidates_flag:
                        self.SendSaliencyMarker(self.name,ts,csaliency_id,"/robot/perception/{}".format(self.name),csaliency.direction)

            # prune the candidate saliencies and remove them if they disappeared
            to_be_removed = []
            prune_before_time = ts - self.saliency_keep_time
            for csaliency_id in self.csaliencies:
                self.csaliencies[csaliency_id].PruneBefore(prune_before_time)
                if len(self.csaliencies[csaliency_id].saliencies) == 0:
                    to_be_removed.append(csaliency_id)
            for key in to_be_removed:
                del self.csaliencies[key]


if __name__ == '__main__':

    rospy.init_node('vision_pipeline')
    node = VisionPipeline()
    rospy.spin()
