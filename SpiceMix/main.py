import os, sys, time, itertools, resource, gc, argparse, re, logging
from util import psutil_process, print_datetime

import numpy as np
import torch

from Model import Model

def parse_arguments():
    parser = argparse.ArgumentParser()

    # dataset
    parser.add_argument(
        '--path2dataset', type=str,
        help='name of the dataset, ../data/<dataset> should be a folder containing a subfolder named \'files\''
    )
    parser.add_argument(
        '-K', type=int, default=20,
        help='Number of metagenes'
    )
    parser.add_argument(
        '--neighbor_suffix', type=str, default='',
        help='Suffix of the name of the file that contains interacting cell pairs'
    )
    parser.add_argument(
        '--expression_suffix', type=str, default='',
        help='Suffix of the name of the file that contains expressions'
    )
    parser.add_argument(
        '--repli_list', type=lambda x: list(map(str, eval(x))),
        help='list of names of the experiments, a Python expression, e.g., "[0,1,2]", "range(5)"'
    )
    parser.add_argument(
        '--use_spatial', type=eval,
        help='list of true/false indicating whether to use the spatial information in each experiment, '
             'a Python expression, e.g., "[True,True]", "[False,False,False]", "[True]*5"'
    )

    parser.add_argument('--random_seed', type=int, default=0)
    parser.add_argument('--random_seed4kmeans', type=int, default=0)

    # training & hyperparameters
    parser.add_argument('--lambda_sigma_x_inverse', type=float, default=1e-4, help='Regularization on Sigma_x^{-1}')
    parser.add_argument('--max_iterations', type=int, default=500, help='Maximum number of outer optimization iteration')
    parser.add_argument('--initial_nmf_iterations', type=int, default=5, help='number of NMF iterations in initialization')
    parser.add_argument(
        '--betas', default=np.ones(1), type=np.array,
        help='Positive weights of the experiments; the sum will be normalized to 1; can be scalar (equal weight) or array-like'
    )

    def parse_device(device):
        if device == "-1" or device == "cpu":
            return "cpu"
        if re.match("\d+$", device):
            return f"cuda:{device}"
        if re.match("cuda:\d+$", device):
            return device

        return "cpu"

    parser.add_argument(
        '--device', type=parse_device, default="cpu", dest="PyTorch_device",
        help="Which GPU to use. The value should be either string of form 'cuda_<GPU id>' "
             "or an integer denoting the GPU id. -1 or 'cpu' for cpu only",
    )

    parser.add_argument('--num_threads', type=int, default=1, help='Number of CPU threads for PyTorch')
    parser.add_argument('--num_processes', type=int, default=1, help='Number of processes')
    parser.add_argument('--result_filename', type=str, default="results.hdf5", help='The name of the h5 file to store results')

    return parser.parse_args()

if __name__ == '__main__':
    np.set_printoptions(linewidth=100000)
    args = parse_arguments()
    logging.basicConfig(level=logging.INFO)

    logging.info(f'pid = {os.getpid()}')

    np.random.seed(args.random_seed)
    logging.info(f'random seed = {args.random_seed}')

    torch.set_num_threads(args.num_threads)

    num_replicates = len(args.repli_list)
    betas = np.broadcast_to(args.betas, [num_replicates]).copy().astype(np.float)
    assert (betas>0).all()
    betas /= betas.sum()

    model = Model(
        PyTorch_device=args.PyTorch_device, 
        path2dataset=args.path2dataset, 
        repli_list=args.repli_list,
        use_spatial=args.use_spatial, 
        neighbor_suffix=args.neighbor_suffix, 
        expression_suffix=args.expression_suffix, 
        K=args.K, 
        lambda_sigma_x_inverse=args.lambda_sigma_x_inverse, 
        betas=betas, 
        prior_x_modes=np.array(['Exponential shared fixed']*len(args.repli_list)), 
        result_filename=args.result_filename
    )

    model.initialize(random_seed4kmeans=args.random_seed4kmeans, initial_nmf_iterations=args.initial_nmf_iterations)

    torch.cuda.empty_cache()
    last_Q = np.nan
    max_iterations = args.max_iterations

    for iteration in range(1, max_iterations+1):
        logging.info(f'{print_datetime()}Iteration {iteration} begins')

        model.estimateWeights(iiter=iteration)
        Q = model.estimateParameters(iiter=iteration)

        logging.info(f'{print_datetime()}Q = {Q:.4f}\tdiff Q = {Q-last_Q:.4e}')
        last_Q = Q
