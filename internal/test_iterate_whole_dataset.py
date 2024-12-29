import argparse
import functools
import getpass
import multiprocessing as mp
import os
import sys
import traceback
import json
import glob
import os
import re
import logging


import torch
from typing import List, Optional
from tqdm import tqdm
from uco3d.data_utils import get_all_load_dataset
from collections import defaultdict


logging.basicConfig(level=logging.INFO)


# To resolve memory leaks giving received 0 items from anecdata
# Reference link https://github.com/pytorch/pytorch/issues/973
torch.multiprocessing.set_sharing_strategy("file_system")
print(torch.multiprocessing.get_all_sharing_strategies())


def iterate_dataset_worker(
    rank: int,
    world_size: int,
    log_dir: str,
    dataset_root: str,
    num_workers: int,
    num_frames_per_batch: int = 16,
    specific_dataset_idx: List[int] = None,
    fast_check: bool = False,
    shuffle_batches_seed: Optional[int] = None,
):

    if fast_check:
        print("running fast check")

    if specific_dataset_idx is not None:
        assert world_size <= 1
        assert rank == 0

    print("loading dataset ...")
    dataset = get_all_load_dataset(
        frame_data_builder_kwargs=dict(
            dataset_root=dataset_root,
            gaussian_splats_load_higher_order_harms=not fast_check,
            gaussian_splats_truncate_background=True,
            apply_alignment=not fast_check,
            load_empty_point_cloud_if_missing=True,
            # load_depths=False,
        ),
        dataset_kwargs=dict(
            subset_lists_file=None,
            subsets=None,
            # sqlite_metadata_file="/fsx-repligen/shared/datasets/uCO3D/dataset_export_tool/temp_database_1217_all/metadata_1734405665.4193773.sqlite",
            # sqlite_metadata_file="/fsx-repligen/shared/datasets/uCO3D/dataset_export_tool/temp_database_1217_all/metadata_1734405665.4193773.sqlite",
            # sqlite_metadata_file="/fsx-repligen/shared/datasets/uCO3D/dataset_export_tool/temp_database_1218_all/metadata.sqlite",
            # sqlite_metadata_file="/fsx-repligen/shared/datasets/uCO3D/dataset_export_tool/temp_database_1221_all/metadata.sqlite",
            # subset_lists_file=os.path.join(
            #     dataset_root,
            #     "set_lists",
            #     "set_lists_all-categories.sqlite",
            #     # "set_lists_3categories-debug.sqlite",
            # ),
            # subsets=["train", "val"],
            # subset_lists_file=None,
        ),  # this will load the whole dataset without any setlists
    )
    print("done loading dataset.")
    assert (
        not dataset.is_filtered()
    ), "Dataset is filtered, this script is for full dataset only"
    all_idx = torch.arange(len(dataset))
    idx_chunk = torch.chunk(all_idx, world_size)
    idx_this_worker = idx_chunk[rank]

    if True:
        # dataset = torch.utils.data.Subset(dataset, idx_this_worker)
        if specific_dataset_idx is not None:
            batch_sampler = [specific_dataset_idx]
        else:
            batch_sampler = [
                b.tolist()
                for b in torch.split(
                    idx_this_worker,
                    num_frames_per_batch,
                )
            ]

        if shuffle_batches_seed is not None:
            torch.manual_seed(shuffle_batches_seed)
            prm = torch.randperm(len(batch_sampler)).tolist()
            batch_sampler = [batch_sampler[prm[i]] for i in range(len(batch_sampler))]

        # resume from checkpoint if needed
        batch_idx_start = _load_worker_checkpoint(log_dir, rank)
        if batch_idx_start is None:
            batch_idx_start = 0
        batch_nums = list(range(len(batch_sampler)))[batch_idx_start:]
        batch_sampler = batch_sampler[batch_idx_start:]

        dataloader = torch.utils.data.DataLoader(
            dataset,
            num_workers=num_workers,
            collate_fn=lambda x: x,
            shuffle=False,
            batch_sampler=batch_sampler,
            prefetch_factor=4 if num_workers > 0 else None,
        )

        assert len(batch_nums) == len(batch_sampler)
        dataloader_iter = iter(dataloader)
        for iter_idx, batch_idx in tqdm(
            enumerate(batch_nums),
            desc=f"worker {rank+1} / {world_size}",
            total=len(batch_nums),
        ):
            # _ = next(dataloader_iter)
            batch_indices = batch_sampler[iter_idx]
            try:
                _ = next(dataloader_iter)
            except Exception as e:
                exc_file = os.path.join(log_dir, f"{batch_indices[0]}_exc.txt")
                print(f"Fail for idx {batch_idx}")
                print(traceback.format_exc())
                with open(exc_file, "w") as f:
                    f.write(traceback.format_exc())

                # get sequence names and paths to the problematic scenes
                try:
                    batch_meta = [
                        dataset.meta[batch_index] for batch_index in batch_indices
                    ]
                    batch_sequences = [
                        (
                            bm.sequence_super_category,
                            bm.sequence_category,
                            bm.sequence_name,
                        )
                        for bm in batch_meta
                    ]
                except Exception as e:
                    print(f"Failed to get batch_sequences for batch {batch_idx}")
                    print(traceback.format_exc())
                    batch_sequences = None
                batch_file = os.path.join(log_dir, f"{batch_indices[0]}_batch.json")
                with open(batch_file, "w") as f:
                    json.dump(
                        {
                            "batch_indices": batch_indices,
                            "batch_sequence_names": batch_sequences,
                        },
                        f,
                    )

            if iter_idx % 100 == 0 and iter_idx > 0:
                _store_worker_checkpoint(log_dir, rank, batch_idx)
    else:
        idx_this_worker = idx_this_worker.tolist()
        for idx in tqdm(idx_this_worker, desc=f"worker {rank} / {world_size}"):
            try:
                _ = dataset[idx]
            except Exception as e:
                exc_file = os.path.join(log_dir, f"{idx:015d}.txt")
                print(f"Fail for idx {idx}")
                print(traceback.format_exc())
                with open(exc_file, "w") as f:
                    f.write(traceback.format_exc())


def _analyze_logs(log_dir):
    exc_files = sorted(glob.glob(os.path.join(log_dir, "*_exc.txt")))
    bad_gauss_splats = []
    missing_segmented_pcls = []

    seq_to_exc = {}
    seq_to_idx = {}

    # step = False

    for exc_file in tqdm(exc_files):
        with open(exc_file, "r") as f:
            exc_string = f.read()
        batch_file = exc_file.replace("_exc.txt", "_batch.json")
        with open(batch_file, "r") as f:
            batch_info = json.load(f)

        # for bidx, seq_name in zip(batch_info["batch_indices"], batch_info["batch_sequence_names"]):
        # if "1-91152-9143" in seq_name[-1]:
        #     step = True
        # if step:
        #     import pdb; pdb.set_trace()

        n_seqs_in_batch = len(set(tuple(s) for s in batch_info["batch_sequence_names"]))
        assert n_seqs_in_batch <= 2

        bix, seq_name = (
            batch_info["batch_indices"][0],
            batch_info["batch_sequence_names"][0],
        )
        if n_seqs_in_batch == 1:
            if tuple(seq_name) not in seq_to_exc:
                seq_to_exc[tuple(seq_name)] = exc_string
                seq_to_idx[tuple(seq_name)] = batch_info["batch_indices"]

        # exc_lines = exc_string.split()

        # if exc_lines[-1].endswith("/gaussian_splats/meta.json'"):
        #     start = exc_lines[-1].rfind("/fsx-repligen/shared/")
        #     splats_folder = exc_lines[-1][start:-1]
        #     bad_gauss_splats.append(splats_folder)
        #     continue

        # if exc_lines[-4].endswith("segmented_point_cloud.ply"):
        #     missing_segmented_pcls.append(exc_lines[-4])
        #     continue

        # # print("\n\n\n\n----------\n\n\n\n")
        # print(exc_string)
        # print(batch_indices)
        # import pdb; pdb.set_trace()
        # pass

    err_description_to_seqs = defaultdict(list)
    for seq, exc in seq_to_exc.items():
        # print("\n\n\n\n----------\n\n\n\n")
        any_match = False
        for err_description, pat in (
            (
                "dataloader fail",
                r"RuntimeError: DataLoader worker (.*) exited unexpectedly",
            ),
            (
                "bad depth_maps file (depth map has wrong shape)",
                r"self\.depth_map = crop_around_box(.*)squashed image",
            ),
            (
                "bad depth_maps file (KeyError)",
                r"h5py\.h5o\.open(.*)KeyError: \"Unable to open object",
            ),
            (
                "bad depth_maps file (KeyError)",
                r"h5py\.h5o\.open(.*)KeyError: \"Unable to synchronously open object",
            ),
            (
                "missing segmented_point_cloud.py",
                r"FileNotFoundError: PointCloud file /fsx-repligen/shared/datasets/uCO3D/dataset_export/(.*)/segmented_point_cloud.ply (.*) does not exist.",
            ),
            (
                "missing gaussian splats meta.json",
                r"FileNotFoundError: \[Errno 2\] No such file or directory: \'/fsx-repligen/shared/datasets/uCO3D/dataset_export/(.*)/gaussian_splats/meta.json\'",
            ),
            (
                "bad CRC-32 for centroids.npy",
                r"zipfile.BadZipFile: Bad CRC-32 for file \'centroids.npy\'",
            ),
            ("cannot load RGB image from video", r"assert image_np is not None"),
            (
                "missing depth_mps.h5",
                r"FileNotFoundError: Depth video /fsx-repligen/shared/datasets/uCO3D/dataset_export/(.*)/depth_maps.h5 does not exist.",
            ),
            (
                "bad shape in gaussian splats meta.json",
                r"load_compressed_gaussians(.*)RuntimeError: shape(.*)is invalid for input of size(.*)",
            ),
            (
                "missing mask_video.mkv",
                r"FileNotFoundError: Video /fsx-repligen/shared/datasets/uCO3D/dataset_export/(.*)/mask_video.mkv does not exist.",
            ),
            ("stop iteration", r"raise StopIteration"),
            ("point cloud path None", r"ValueError: Point cloud path is None."),
            (
                "depth maps path None",
                r"depth_h5_path = os.path.join(.*)argument must be str, bytes, or",
            ),
            (
                "mask video path None",
                r"fg_mask_np = self\._frame_from_video(.*)argument must be str, bytes, or",
            ),
            (
                "mask video corrupt",
                r"ValueError: Cannot load mask frame from",
            ),
            (
                "worker killed",
                r"RuntimeError: DataLoader worker (.*) is killed by signal: Killed",
            ),
        ):
            match = re.compile(pat).search(exc.replace("\n", ""))
            if match is not None:
                # print(match.groups())
                any_match = True
                err_description_to_seqs[err_description].append(seq)
                if "stop iteration" in err_description:
                    print(seq_to_idx[seq])
                    import pdb

                    pdb.set_trace()
                    pass

                if "missing gaussian splats meta.json" in err_description:
                    meta_file = exc.split()[-1][1:-1]
                    if "/".join(seq) not in meta_file:
                        err_description_to_seqs[err_description].pop()
                    # assert "/".join(seq) in meta_file, f"{'/'.join(seq)}, {meta_file}"
                    # print(f"{seq}: {meta_file}")
                    # import pdb; pdb.set_trace()
                    # if "1-91152-9143" in meta_file:
                    #     import pdb; pdb.set_trace()
                    #     pass
                    assert not os.path.isfile(meta_file)

                if "missing segmented_point_cloud.py" in err_description:
                    pcl_file = str(match.groups()[1][3:])
                    assert not os.path.isfile(pcl_file)

        if not any_match:
            print(exc)
            print(seq_to_idx[seq])
            print(seq)
            import pdb

            pdb.set_trace()
            pass

    print("\n\n\n\n----------\n\n\n\n")
    err_description_to_seqs = dict(err_description_to_seqs)
    for err_description, seqs in err_description_to_seqs.items():
        print(err_description)
        for seq in seqs:
            print("     " + "/".join(seq))

    # for missing_segmented_pcl in missing_segmented_pcls:
    #     print(missing_segmented_pcl)

    # bad_gauss_splats = sorted(list(set(bad_gauss_splats)))
    # for splats_folder in bad_gauss_splats:
    #     print(splats_folder)
    # import pdb; pdb.set_trace()


def _get_worker_checkpoint_file(log_dir, rank):
    return os.path.join(log_dir, f"worker_{rank}_checkpoint.txt")


def _store_worker_checkpoint(log_dir, rank, batch_idx):
    checkpoint_file = _get_worker_checkpoint_file(log_dir, rank)
    print(
        f"Storing checkpoint for worker {rank} at batch {batch_idx}: {checkpoint_file}"
    )
    with open(checkpoint_file, "w") as f:
        f.write(f"{batch_idx}")


def _load_worker_checkpoint(log_dir, rank):
    checkpoint_file = _get_worker_checkpoint_file(log_dir, rank)
    if os.path.exists(checkpoint_file):
        with open(checkpoint_file, "r") as f:
            batch_idx = int(f.read())
        print(
            f"Loading checkpoint for worker {rank} at batch {batch_idx}: {checkpoint_file}"
        )
        return batch_idx
    else:
        return None


if __name__ == "__main__":
    argparse = argparse.ArgumentParser()
    argparse.add_argument("--dataset_root", type=str, default=None)
    argparse.add_argument("--log_dir", type=str, required=True)
    argparse.add_argument("--world_size", type=int, default=4)
    argparse.add_argument("--num_workers", type=int, default=4)
    argparse.add_argument("--shuffle_batches_seed", type=int, default=None)
    argparse.add_argument("--run_locally", action="store_true")
    argparse.add_argument("--analyze_logs", action="store_true")
    argparse.add_argument("--fast_check", action="store_true")
    args = argparse.parse_args()

    # print the argparse args
    print(args)

    if bool(args.analyze_logs):
        _analyze_logs(str(args.log_dir))
        sys.exit(0)

    os.makedirs(str(args.log_dir), exist_ok=True)

    # broken depth h5:
    # [10577174, 10577175, 10577176, 10577177, 10577178, 10577179, 10577180, 10577181, 10577182, 10577183, 10577184, 10577185, 10577186, 10577187, 10577188, 10577189]

    if bool(args.run_locally):

        world_size = int(args.world_size)
        if world_size <= 0:
            iterate_dataset_worker(
                0,
                1,
                log_dir=str(args.log_dir),
                dataset_root=str(args.dataset_root),
                num_workers=int(args.num_workers),
                fast_check=bool(args.fast_check),
                shuffle_batches_seed=int(args.shuffle_batches_seed),
                # specific_dataset_idx=[19679710, 19679711, 19679712, 19679713, 19679714, 19679715, 19679716, 19679717, 19679718, 19679719, 19679720, 19679721, 19679722, 19679723, 19679724],
                # specific_dataset_idx=[25894583, 25894584, 25894585, 25894586, 25894587, 25894588, 25894589, 25894590, 25894591, 25894592, 25894593, 25894594, 25894595, 25894596, 25894597, 25894598],
                # specific_dataset_idx=[10576774, 10576775, 10576776, 10576777, 10576778, 10576779, 10576780, 10576781, 10576782, 10576783, 10576784, 10576785, 10576786, 10576787, 10576788, 10576789],
                # specific_dataset_idx=[10577174, 10577175, 10577176, 10577177, 10577178, 10577179, 10577180, 10577181, 10577182, 10577183, 10577184, 10577185, 10577186, 10577187, 10577188, 10577189],  # weird depth
                # specific_dataset_idx=[13548035, 13548036, 13548037, 13548038, 13548039, 13548040, 13548041, 13548042, 13548043, 13548044, 13548045, 13548046, 13548047, 13548048, 13548049, 13548050],  # bad crc
                # specific_dataset_idx=[16606624, 16606625, 16606626, 16606627, 16606628, 16606629, 16606630, 16606631, 16606632, 16606633, 16606634, 16606635, 16606636, 16606637, 16606638, 16606639],  # bad crc
                # specific_dataset_idx=[17683231, 17683232, 17683233, 17683234, 17683235, 17683236, 17683237, 17683238, 17683239, 17683240, 17683241, 17683242, 17683243, 17683244, 17683245, 17683246],  # bad crc
                # specific_dataset_idx=[22040411, 22040412, 22040413, 22040414, 22040415, 22040416, 22040417, 22040418, 22040419, 22040420, 22040421, 22040422, 22040423, 22040424, 22040425, 22040426],  # bad crc
                # [24957512, 24957513, 24957514, 24957515, 24957516, 24957517, 24957518, 24957519, 24957520, 24957521, 24957522, 24957523, 24957524, 24957525, 24957526, 24957527]  # cannot get image from dataloader
                # [26304631, 26304632, 26304633, 26304634, 26304635, 26304636, 26304637, 26304638, 26304639, 26304640, 26304641, 26304642, 26304643, 26304644, 26304645, 26304646]  # bad gaussian shape
            )
        else:
            with mp.get_context("spawn").Pool(processes=world_size) as pool:
                worker = functools.partial(
                    iterate_dataset_worker,
                    world_size=world_size,
                    log_dir=str(args.log_dir),
                    dataset_root=str(args.dataset_root),
                    num_workers=int(args.num_workers),
                    fast_check=bool(args.fast_check),
                    shuffle_batches_seed=int(args.shuffle_batches_seed),
                )
                pool.map(worker, list(range(world_size)))

    else:
        from griddle.submitit_jobs import submitit_jobs

        username = getpass.getuser()
        user_slurm_log_dir = f"/fsx-repligen/{username}/slurm_jobs_uco3d/"
        os.makedirs(user_slurm_log_dir, exist_ok=True)
        root_job_name = "iterate_uco3d"
        debug = False

        kwargs_list = [
            {
                "rank": i,
                "world_size": int(args.world_size),
                "log_dir": str(args.log_dir),
                "dataset_root": str(args.dataset_root),
                "num_workers": int(args.num_workers),
                "fast_check": bool(args.fast_check),
                "shuffle_batches_seed": int(args.shuffle_batches_seed),
            }
            for i in range(int(args.world_size))
        ]

        submitit_jobs(
            iterate_dataset_worker,
            kwargs_list,
            root_job_name=root_job_name,
            slurm_dir=user_slurm_log_dir,
            slurm_gpus_per_task=2,
            slurm_cpus_per_gpu=int(args.num_workers) + 1,
            slurm_ntasks_per_node=1,
            nodes=1,
            mem_per_cpu=16,
            slurm_time=3600,
            slurm_partition="learn",
            slurm_account="repligen",
            slurm_qos="low",
            debug=debug,
            disable_job_state_monitor=False,
            slurm_array_parallelism=32,
        )


# RUNS:
# python ./test_iterate_whole_dataset.py --run_locally --world_size 0 --log_dir="$HOME/data/uco3d_iterate_log_241213/" --dataset_root="$HOME/data//"
# python ./test_iterate_whole_dataset.py --run_locally --world_size 0 --log_dir="/fsx-repligen/dnovotny/datasets/uCO3D/uco3d_iterate_log_241213_debug/" --dataset_root="/fsx-repligen/shared/datasets/uCO3D/dataset_export/" --num_workers 4
# python ./test_iterate_whole_dataset.py --world_size 32 --log_dir="/fsx-repligen/dnovotny/datasets/uCO3D/uco3d_iterate_log_241213_2/" --dataset_root="/fsx-repligen/shared/datasets/uCO3D/dataset_export/" --num_workers 16
# python ./test_iterate_whole_dataset.py --world_size 32 --log_dir="/fsx-repligen/dnovotny/datasets/uCO3D/uco3d_iterate_log_241215/" --dataset_root="/fsx-repligen/shared/datasets/uCO3D/dataset_export/" --num_workers 16
# python ./test_iterate_whole_dataset.py --world_size 0 --log_dir="/fsx-repligen/dnovotny/datasets/uCO3D/uco3d_iterate_log_241215_debug/" --dataset_root="/fsx-repligen/shared/datasets/uCO3D/dataset_export/" --num_workers 16 --run_locally
# python ./test_iterate_whole_dataset.py --world_size 0 --log_dir="/fsx-repligen/dnovotny/datasets/uCO3D/uco3d_iterate_log_241216_debug/" --dataset_root="/fsx-repligen/shared/datasets/uCO3D/dataset_export/" --num_workers 0 --run_locally
# python ./test_iterate_whole_dataset.py --world_size 0 --log_dir="/fsx-repligen/dnovotny/datasets/uCO3D/uco3d_iterate_log_241217_debug/" --dataset_root="/fsx-repligen/shared/datasets/uCO3D/dataset_export/" --num_workers 0 --run_locally
# python ./test_iterate_whole_dataset.py --world_size 32 --log_dir="/fsx-repligen/dnovotny/datasets/uCO3D/uco3d_iterate_log_241218/" --dataset_root="/fsx-repligen/shared/datasets/uCO3D/dataset_export/" --num_workers 16
# python ./test_iterate_whole_dataset.py --world_size 32 --log_dir="/fsx-repligen/dnovotny/datasets/uCO3D/uco3d_iterate_log_241218_newsqlite/" --dataset_root="/fsx-repligen/shared/datasets/uCO3D/dataset_export/" --num_workers 16
# python ./test_iterate_whole_dataset.py --world_size 0 --log_dir="/fsx-repligen/dnovotny/datasets/uCO3D/uco3d_iterate_log_241218_newsqlite_debug/" --dataset_root="/fsx-repligen/shared/datasets/uCO3D/dataset_export/" --num_workers 0 --run_locally
# python ./test_iterate_whole_dataset.py --world_size 32 --log_dir="/fsx-repligen/dnovotny/datasets/uCO3D/uco3d_iterate_log_241219/" --dataset_root="/fsx-repligen/shared/datasets/uCO3D/dataset_export/" --num_workers 16 --fast_check
# python ./test_iterate_whole_dataset.py --world_size 32 --log_dir="/fsx-repligen/dnovotny/datasets/uCO3D/uco3d_iterate_log_241221/" --dataset_root="/fsx-repligen/shared/datasets/uCO3D/dataset_export/" --num_workers 16 --fast_check
# python ./test_iterate_whole_dataset.py --world_size 32 --log_dir="/fsx-repligen/dnovotny/datasets/uCO3D/uco3d_iterate_log_241221_downloaded_dataset/" --dataset_root="/fsx-repligen/shared/datasets/uCO3D/full_download_test/" --num_workers 16 --fast_check
# python ./test_iterate_whole_dataset.py --world_size 0 --log_dir="/fsx-repligen/dnovotny/datasets/uCO3D/uco3d_iterate_log_241223_downloaded_dataset_shuf/" --dataset_root="/fsx-repligen/shared/datasets/uCO3D/full_download_test/" --num_workers 16 --fast_check --shuffle_batches_seed 42
# python ./test_iterate_whole_dataset.py --world_size 40 --log_dir="/fsx-repligen/dnovotny/datasets/uCO3D/uco3d_iterate_log_241223_downloaded_dataset_shuf/" --dataset_root="/fsx-repligen/shared/datasets/uCO3D/full_download_test/" --num_workers 16 --fast_check --shuffle_batches_seed 42
