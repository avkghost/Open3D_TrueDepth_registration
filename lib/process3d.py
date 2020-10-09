import sys
import glob
import copy
import math
import time
import numpy as np
from scipy.spatial.transform import Rotation
import open3d as o3d
import matplotlib.pyplot as plt
import cv2 as cv
from mpl_toolkits.axes_grid1 import ImageGrid
from . rigid_transform_3D import rigid_transform_3D
from . image_depth import ImageDepth
from cpp.pose_graph import pose_graph

def process3d(args):
    image_files = sorted(glob.glob(f"{args.folder}/video*.bin"))#[0:3]
    depth_files = sorted(glob.glob(f"{args.folder}/depth*.bin"))#[0:3]
    calibration_file =f"{args.folder}/calibration.json"

    if len(image_files) == 0:
        print("No image files found")
        sys.exit(0)

    if len(depth_files) == 0:
        print("No depth files found")
        sys.exit(0)

    # generate some colors for the point cloud
    val = np.arange(len(depth_files)) / len(depth_files)
    colors = plt.cm.jet(val)
    colors = colors[:, 0:3]

    # final global point cloud
    global_pcd = None
    point_clouds = []

    # load all the point clouds into memory
    for i, (image_file, depth_file) in enumerate(zip(image_files, depth_files)):
        obj = ImageDepth(
            calibration_file,
            image_file,
            depth_file,
            args.width,
            args.height,
            args.min_depth,
            args.max_depth,
            args.normal_radius)

        obj.pcd.paint_uniform_color(colors[i])

        point_clouds.append(obj)


    sift = cv.SIFT_create()

    # run SIFT on all the images
    for cur in point_clouds:
        print(f"Running SIFT on {cur.image_file}")
        cur.kp, cur.desc = sift.detectAndCompute(cur.gray_undistort, None)

    all_matches = dict()

    # match all pairings
    # NOTE: This is highly parallelisable!!!
    for i in range(0, len(point_clouds)):
        all_matches[i] = dict()
        pc_i = point_clouds[i]

        if args.max_look_ahead > len(point_clouds) or args.max_look_ahead <= 0:
            # all matches
            js = range(i+1, len(point_clouds))
        else:
            js = []
            for k in range(0, args.max_look_ahead):
                js.append((i+1+k) % len(point_clouds))

        for j in js:
            pc_j = point_clouds[j]
            i_pts, j_pts = find_sift_matches(pc_i.kp, pc_i.desc, pc_j.kp, pc_j.desc)

            # geometric constraint
            _, mask = cv.findFundamentalMat(np.array(i_pts), np.array(j_pts), cv.FM_RANSAC, 3.0)

            mask = mask.squeeze()
            i_pts = np.array(i_pts)[np.where(mask)]
            j_pts = np.array(j_pts)[np.where(mask)]

            # find common good points
            i_3d, i_2d, i_good_idx = pc_i.project3d(i_pts)
            j_3d, j_2d, j_good_idx = pc_j.project3d(j_pts)
            good_idx = np.intersect1d(i_good_idx, j_good_idx)

            i_3d = i_3d[good_idx]
            i_2d = i_2d[good_idx]
            j_3d = j_3d[good_idx]
            j_2d = j_2d[good_idx]

            all_matches[i][j] = (i_2d, i_3d, j_2d, j_3d)

            print(f"Matching {pc_i.image_file} {pc_j.image_file}, matches: {len(i_3d)}")

    global_pcd = point_clouds[0].pcd
    lase_pose = np.eye(4,4)

    # run sequential registration to initialize the camera pose
    for i in range(1, len(point_clouds)):
        prev = point_clouds[i-1]
        cur = point_clouds[i]

        print(cur.image_file, cur.depth_file)

        delta_pose = np.eye(4, 4)

        if args.vis_tracking:
            prev_2d, prev_3d, cur_2d, cur_3d = all_matches[i-1][i]

            R, t, rmse = rigid_transform_3D(cur_3d.transpose(), prev_3d.transpose())
            delta_pose[0:3, 0:3] = R
            delta_pose[0:3, 3:4] = t
            #print("vision delta_pose")
            print(delta_pose)
            #print("vision rmse:", rmse)
            img = cv.cvtColor(cur.gray_undistort, cv.COLOR_GRAY2BGR)

            # draw matches
            for a, b in zip(prev_2d, cur_2d):
                aa = (a[0].item(), a[1].item())
                bb = (b[0].item(), b[1].item())
                img = cv.line(img, aa, bb, (0,0,255))

            if args.viz:
                cv.imshow("cur", img)
                cv.waitKey(50)

        pose = lase_pose @ delta_pose

        if args.seq_icp:
            reg = o3d.registration.registration_icp(
                cur.pcd,
                global_pcd,
                args.distance_threshold,
                pose,
                o3d.registration.TransformationEstimationPointToPlane())

            pose = reg.transform

        # apply transform and merge point cloud
        cur.pose = pose
        lase_pose = pose

    # setup pose graph for optimization
    poses = np.zeros((len(point_clouds), 7))
    for i, p in enumerate(point_clouds):
        r = Rotation.from_matrix(p.pose[0:3,0:3])
        r = r.as_quat()

        qx = r[0]
        qy = r[1]
        qz = r[2]
        qw = r[3]

        poses[i, 0:4] = np.array([qw, qx, qy, qz])
        poses[i, 4:7] = p.pose[0:3,3]

    matches = np.zeros((0, 8))
    for idx1 in all_matches:
        for idx2 in all_matches[idx1]:
            i_2d, i_3d, j_2d, j_3d = all_matches[idx1][idx2]

            if len(i_3d) > args.min_matches:
                for p, c in zip(i_3d, j_3d):
                    r = np.array([idx1, idx2, p[0], p[1], p[2], c[0], c[1], c[2]])
                    matches = np.vstack((matches, r))

    optim_poses = pose_graph(poses, matches)

    for i, pc in enumerate(point_clouds):
        qw, qx, qy, qz = optim_poses[i][0:4]
        tx, ty, tz = optim_poses[i][4:7]

        r = Rotation.from_quat([qx, qy, qz, qw])

        transform = np.eye(4,4)
        transform[0:3,0:3] = r.as_matrix()
        transform[0,3] = tx
        transform[1,3] = ty
        transform[2,3] = tz

        #print(transform)
        pc.pcd.transform(transform)

        if global_pcd is None:
            global_pcd = pc.pcd
        else:
            global_pcd += pc.pcd

    # save the points
    # remove normals to save space
    #empty_array = np.zeros((1,3), dtype=np.float64)
    #global_pcd.normals = o3d.utility.Vector3dVector(empty_array)

    print(f"Saving to {args.output} ...")
    o3d.io.write_point_cloud(args.output, global_pcd)

    if args.viz:
        custom_draw_geometry([global_pcd])

def custom_draw_geometry(pcd, name="Open3D"):
    vis = o3d.visualization.Visualizer()
    vis.create_window(name)

    for p in pcd:
        vis.add_geometry(p)

    axis = o3d.geometry.TriangleMesh.create_coordinate_frame(size=0.1, origin=[0, 0, 0])
    vis.add_geometry(axis)

    opt = vis.get_render_option()
    opt.background_color = np.asarray([0, 0, 0])
    opt.point_size = 1.0

    vis.run()
    vis.destroy_window()

def find_sift_matches(prev_kp, prev_desc, cur_kp, cur_desc):
    FLANN_INDEX_KDTREE = 0
    index_params = dict(algorithm=FLANN_INDEX_KDTREE, trees=5)
    search_params = dict(checks=50)

    flann = cv.FlannBasedMatcher(index_params, search_params)

    knn_matches = flann.knnMatch(prev_desc, cur_desc, k=2)

    prev_pts = []
    cur_pts = []

    for m in knn_matches:
        prev_idx = m[0].queryIdx

        dist1 = m[0].distance
        dist2 = m[1].distance

        # sift ratio test
        if dist1 < dist2*0.7:
            cur_idx = m[0].trainIdx

            prev_pts.append(prev_kp[prev_idx].pt)
            cur_pts.append(cur_kp[cur_idx].pt)

    return prev_pts, cur_pts
