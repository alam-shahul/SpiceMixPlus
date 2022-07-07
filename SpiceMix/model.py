import sys, time, itertools, resource, logging, h5py, os, pickle
from pathlib import Path
import multiprocessing
from multiprocessing import Pool
from util import print_datetime, openH5File, encode4h5, parse_suffix

import numpy as np
import pandas as pd
from scipy.special import factorial

import torch
import anndata as ad

from load_data import load_expression, load_edges, load_genelist, load_anndata, save_anndata, save_predictors
from initialization import initialize_kmeans, initialize_Sigma_x_inv, initialize_svd
from estimate_weights import estimate_weight_wonbr, estimate_weight_wnbr, \
    estimate_weight_wnbr_phenotype, estimate_weight_wnbr_phenotype_v2
from estimate_parameters import estimate_M, estimate_Sigma_x_inv, estimate_phenotype_predictor
from sample_for_integral import integrate_of_exponential_over_simplex

class SpiceMixPlus:
    """SpiceMixPlus optimization model.

    Models spatial biological data using the NMF-HMRF formulation. Supports multiple
    fields-of-view (FOVs) and multimodal data.

    Attributes:
        device: device to use for PyTorch operations
        num_processes: number of parallel processes to use for optimizing weights (should be <= #FOVs)
        replicate_names: names of replicates/FOVs in input dataset
        TODO: finish docstring
    """
    def __init__(self,
            K, lambda_Sigma_x_inv, repli_list, betas=None, prior_x_modes=None,
            path2result=None, context=None, context_Y=None, metagene_mode="shared", Sigma_x_inv_mode="shared",
            lambda_M=0, random_state=0
    ):

        if not context:
            context = dict(device='cpu', dtype=torch.float32)
        if not context_Y:
            context_Y = context

        self.context = context
        self.context_Y = context_Y
        self.repli_list = repli_list
        self.num_repli = len(self.repli_list)

        self.random_state = random_state
        torch.manual_seed(self.random_state)
        np.random.seed(self.random_state)

        self.K = K
        self.lambda_Sigma_x_inv = lambda_Sigma_x_inv
        if betas is None:
            self.betas = np.full(self.num_repli, 1/self.num_repli)
        else:
            self.betas = np.array(betas, copy=False) / sum(betas)

        if prior_x_modes is None:
            prior_x_modes = ['exponential shared fixed'] * self.num_repli

        self.prior_x_modes = prior_x_modes
        self.M_constraint = 'simplex'
        # self.M_constraint = 'unit sphere'
        self.X_constraint = 'none'
        self.dropout_mode = 'raw'
        self.sigma_yx_inv_mode = 'separate'
        self.pairwise_potential_mode = 'normalized'

        if path2result is not None:
            self.path2result = path2result
            self.result_filename = self.path2result
            logging.info(f'result file = {self.result_filename}')
        else:
            self.result_filename = None
        # self.save_hyperparameters()

        self.Ys = None
        self.Sigma_x_inv = self.Xs = self.sigma_yxs = None
       
        self.metagene_mode = metagene_mode 
        self.Sigma_x_inv_mode = Sigma_x_inv_mode 
        self.M = None
        self.M_bar = None
        self.lambda_M = lambda_M
        # self.Zs = self.Ss = self.Z_optimizers = None
        # self.optimizer_Sigma_x_inv = None
        self.prior_xs = None
        self.phenotypes = self.phenotype_predictors = None
        # self.meta_repli = None

    def load_dataset(self, path2dataset, data_format="raw", anndata_filepath=None, neighbor_suffix=None, expression_suffix=None):
        """Load dataset into SpiceMix object.

        TODO: change this to accept anndata objects.
        """

        path2dataset = Path(path2dataset)
        neighbor_suffix = parse_suffix(neighbor_suffix)
        expression_suffix = parse_suffix(expression_suffix)
        
        if data_format == "anndata":
            self.datasets = load_anndata(path2dataset / anndata_filepath, self.repli_list, self.context)
            self.Ys = []
            for dataset in self.datasets:
                Y = torch.tensor(dataset.X, **self.context_Y)
                self.Ys.append(Y)
        if data_format == "raw":
            self.raw_Ys = []
            for replicate in self.repli_list:
                for s in ['pkl', 'txt', 'tsv', 'pickle']:
                    path2file = path2dataset / 'files' / f'expression_{replicate}{expression_suffix}.{s}'
                    if not path2file.exists():
                        continue

                    self.raw_Ys.append(load_expression(path2file))
                    break
            assert len(self.raw_Ys) == len(self.repli_list)
            self.Ns, self.Gs = zip(*map(np.shape, self.raw_Ys))
            self.GG = max(self.Gs)
            self.genes =  [
                load_genelist(path2dataset / 'files' / f'genes_{r}{expression_suffix}.txt')
                for r in self.repli_list
            ]

            self.Es = [
                load_edges(path2dataset / 'files' / f'neighborhood_{i}{neighbor_suffix}.txt', N)
                for i, N in zip(self.repli_list, self.Ns)
            ]
            self.Es_isempty = [sum(map(len, E)) == 0 for E in self.Es]

            self.datasets = []
            self.Ys = []
            for index, (raw_gene_expression, gene_list, n_obs, replicate, adjacency_list) in enumerate(zip(self.raw_Ys, self.genes, self.Ns, self.repli_list, self.Es)):
                gene_expression = len(gene_list) / self.GG * self.K * raw_gene_expression / raw_gene_expression.sum(axis=1).mean()
                gene_expression = torch.tensor(gene_expression, **self.context_Y)
                if self.context_Y.get("device", "cpu") == "cpu":
                    gene_expression = gene_expression.pin_memory()

                self.Ys.append(gene_expression)
                raw_dataframe = pd.DataFrame(raw_gene_expression, columns=gene_list, index=np.arange(len(gene_expression), dtype=int).astype(str))
                dataset = ad.AnnData(raw_dataframe)
                dataset.raw = dataset
                dataset.X = gene_expression.detach().cpu().numpy()

                def get_adjacency_matrix(adjacency_list):
                    """ According to the context, create an adjacency matrix from an adjacency list

                    """

                    edges = [(i, j) for i, e in enumerate(adjacency_list) for j in e]
                    adjacency_matrix = torch.sparse_coo_tensor(np.array(edges).T, np.ones(len(edges)), size=(len(adjacency_list), len(adjacency_list)), **self.context)
                    
                    return adjacency_matrix

                adjacency_list = load_edges(path2dataset / 'files' / f'neighborhood_{replicate}{neighbor_suffix}.txt', n_obs)
                coordinates = np.loadtxt(path2dataset / 'files' / f'coordinates_{replicate}.txt')
                adjacency_matrix = get_adjacency_matrix(adjacency_list)
                
                dataset.obsp["adjacency_matrix"] = adjacency_matrix
                dataset.obs["adjacency_list"] = adjacency_list
                dataset.obsm["spatial"] = coordinates
                dataset.obs["replicate"] = np.full(n_obs, replicate, dtype=str)
                dataset.uns["Sigma_x_inv"] = {f"{replicate}": None}
                
                self.datasets.append(dataset)

            # self.Ys = [G / self.GG * self.K * Y / Y.sum(1).mean() for Y, G in zip(self.Ys, self.Gs)]
            # self.Ys = [
            #     torch.tensor(Y, **self.context_Y).pin_memory()
            #     if self.context_Y.get('device', 'cpu') == 'cpu' else
            #     torch.tensor(Y, **self.context_Y)
            #     for Y in self.Ys
            # ]
            # TODO: save Ys in cpu and calculate matrix multiplications (e.g., MT Y) using mini-batch.
            for r, dataset in zip(self.repli_list, self.datasets):
                path2file = path2dataset / 'files' / f'meta_{r}.pkl'
                if os.path.exists(path2file):
                    with open(path2file, 'rb') as f:
                        data = pickle.load(f)
                path2file = path2dataset / 'files' / f'meta_{r}.csv'
                if os.path.exists(path2file):
                    continue
                path2file = path2dataset / 'files' / f'celltypes_{r}.txt'
                if os.path.exists(path2file):
                    df = pd.read_csv(path2file, header=None)
                    df.columns = ['cell type']
                    dataset.obs["cell_type"] = df.values
                    # df['repli'] = r
                    continue
                raise FileNotFoundError(r)

        self.phenotypes = [{} for i in range(self.num_repli)]
        self.phenotype_predictors = {}

        if data_format != "anndata":
            pass
            # save_anndata(path2dataset / "all_data.h5", self.datasets, self.repli_list)

    def register_phenotype_predictors(self, phenotype2predictor):
        """Register a phenotype and its predictor to the SpiceMixPlus object.
        
        Args:
            phenotype2predictor: a dictionary. Each key is a column name in self.meta. The corresponding value is a
            3-element tuple that contains:
                1) a predictor, an instance of torch.nn.Module
                2) an optimizer
                3) a loss function
        """
        for phenotype_name, (predictor, optimizer, loss_function) in phenotype2predictor.items():
            self.register_phenotype_predictor(phenotype_name, predictor, optimizer, loss_function)

    def register_phenotype_predictor(self, phenotype_name, predictor, optimizer, loss_function):
        """Register a phenotype and its predictor to the SpiceMixPlus object.
        
        Args:
            phenotype2predictor: a dictionary. Each key is a column name in self.meta. The corresponding value is a
            3-element tuple that contains:
                1) a predictor, an instance of torch.nn.Module
                2) an optimizer
                3) a loss function
        """
        self.phenotype_predictors[phenotype_name] = (predictor, optimizer, loss_function)
        for param in predictor.parameters():
            param.requires_grad_(False)
        
        # for (repli, df) in self.meta.groupby('repli'):
        #     phenotypes = {}
        #     phenotype = torch.tensor(df[phenotype_name].values, device=self.context.get('device', 'cpu'))
        #     self.phenotypes[self.repli_list.index(repli)][phenotype_name] = phenotype
        
        for (repli, dataset) in zip(self.repli_list, self.datasets):
            phenotypes = {}
            phenotype = torch.tensor(dataset.obs[phenotype_name].values, device=self.context.get('device', 'cpu'))
            self.phenotypes[self.repli_list.index(repli)][phenotype_name] = phenotype

    def register_meta_repli(self, df_meta_repli):
        self.meta_repli = df_meta_repli

    def initialize(self, method='kmeans'):
        if self.phenotype_predictors:
            key = next(iter(self.phenotype_predictors.keys()))
            set_of_labels = np.unique(sum([phenotype[key].cpu().numpy().tolist() for phenotype in self.phenotypes if phenotype[key] is not None], []))
           
            # Seeding some metagenes to explain the phenotype(s)
            L = len(set_of_labels)
            M = torch.zeros([self.GG, L], **self.context)
            num_by_class = torch.zeros([L], **self.context)
            Xs = [torch.zeros([N, L], **self.context) for N in self.Ns]
            is_valid = lambda Y, phenotype: Y.shape[1] == M.shape[0] and phenotype[key] is not None
            for phenotype, dataset, X, Y in zip(self.phenotypes, self.datasets, Xs, self.Ys):
                if not is_valid(dataset.X, phenotype):
                    continue

                for i, c in enumerate(set_of_labels):
                    mask = phenotype[key] == c
                    M[:, i] += Y[mask].sum(axis=0)
                    num_by_class[i] += mask.sum()
                    X[mask, i] = 1

            M /= num_by_class

            Ys = []
            for phenotype, dataset, Y in zip(self.phenotypes, self.datasets, self.Ys):
                if not is_valid(dataset.X, phenotype):
                    continue

                Y = Y.clone()
                for i, c in enumerate(set_of_labels):
                    mask = phenotype[key] == c
                    Y[mask] -= M[:, i]
                Ys.append(Y)

            M_res, Xs_res = initialize_svd(self.K - L, Ys, context=self.context)
            self.M = torch.cat([M, M_res], dim=1)
            self.Xs = []
            Xs_res_iter = iter(Xs_res)
            for phenotype, dataset, Y, X in zip(self.phenotypes, self.datasets, self.Ys, Xs):
                if is_valid(Y, phenotype):
                    # If phenotype information is available, use seeded hidden states
                    self.Xs.append(torch.cat([X, next(Xs_res_iter)], dim=1))
                else:
                    # Otherwise, use least squares to fit X to initialized M
                    self.Xs.append(torch.linalg.lstsq(self.M, Y.T)[0].clip_(min=0).T.contiguous())

        elif method == 'kmeans':
            self.M, self.Xs = initialize_kmeans(
                self.K, self.Ys,
                kwargs_kmeans=dict(random_state=self.random_state),
                context=self.context,
            )
        elif method == 'svd':
            self.M, self.Xs = initialize_svd(
                self.K, self.Ys, context=self.context,
                M_nonneg=self.M_constraint == 'simplex', X_nonneg=True,
            )
        else:
            raise NotImplementedError

        if self.M_constraint == 'simplex':
            scale_factor = torch.linalg.norm(self.M, axis=0, ord=1, keepdim=True)
        elif self.M_constraint == 'unit_sphere':
            scale_factor = torch.linalg.norm(self.M, axis=0, ord=2, keepdim=True)
        else:
            raise NotImplementedError
        
        self.M.div_(scale_factor)
        for X in self.Xs:
            X.mul_(scale_factor)

        self.M_bar = self.M
        self.Sigma_x_inv_bar = None

        if all(prior_x_mode == 'exponential shared fixed' for prior_x_mode in self.prior_x_modes):
            # TODO: try setting to zero
            self.prior_xs = [(torch.ones(self.K, **self.context),) for _ in range(self.num_repli)]
        elif all(prior_x_mode == None for prior_x_mode in self.prior_x_modes):
            self.prior_xs = [(torch.zeros(self.K, **self.context),) for _ in range(self.num_repli)]
        else:
            raise NotImplementedError

        for dataset, X, replicate, prior_x in zip(self.datasets, self.Xs, self.repli_list, self.prior_xs):
            dataset.uns["M"] = {f"{replicate}": self.M}
            dataset.uns["M_bar"] = {f"{replicate}": self.M_bar}
            dataset.obsm["X"] = X

            print(f"Saved prior_x: {prior_x}")
            dataset.uns["spicemixplus_hyperparameters"] = {
                "metagene_mode": self.metagene_mode,
                "prior_x": prior_x[0],
                "K": self.K,
                "lambda_Sigma_x_inv": self.lambda_Sigma_x_inv,
                "d": 4,
            }

        self.estimate_sigma_yx()
        self.estimate_phenotype_predictor()

    def initialize_Sigma_x_inv(self):
        if self.Sigma_x_inv_mode == "differential":
            Sigma_x_invs = initialize_Sigma_x_inv(self.K, self.Xs, self.betas, self.context, self.datasets)
            self.Sigma_x_inv_bar = Sigma_x_invs.mean(axis=0)
            for replicate, dataset, Sigma_x_inv in zip(self.repli_list, self.datasets, Sigma_x_invs):
                if "Sigma_x_inv" not in dataset.uns:
                    dataset.uns["Sigma_x_inv"] = {}
                    dataset.uns["Sigma_x_inv_bar"] = {}

                dataset.uns["Sigma_x_inv"][f"{replicate}"] = Sigma_x_inv
                dataset.uns["Sigma_x_inv_bar"][f"{replicate}"] = self.Sigma_x_inv_bar
            # This optimizer retains its state throughout the optimization
            self.optimizer_Sigma_x_invs = []
            for Sigma_x_inv in Sigma_x_invs:
                optimizer = torch.optim.Adam(
                    [Sigma_x_inv],
                    lr=1e-1,
                    betas=(.5, .9),
                )
                self.optimizer_Sigma_x_invs.append(optimizer)

        elif self.Sigma_x_inv_mode == "shared":
            Sigma_x_invs = initialize_Sigma_x_inv(self.K, self.Xs, self.betas, self.context, self.datasets)
            self.Sigma_x_inv = Sigma_x_invs.mean(axis=0)
            for replicate, dataset in zip(self.repli_list, self.datasets):
                if "Sigma_x_inv" not in dataset.uns:
                    dataset.uns["Sigma_x_inv"] = {}

                dataset.uns["Sigma_x_inv"][f"{replicate}"] = self.Sigma_x_inv
            
            # This optimizer retains its state throughout the optimization
            self.optimizer_Sigma_x_inv = torch.optim.Adam(
                [self.Sigma_x_inv],
                lr=1e-1,
                betas=(.5, .9),
            )

    def estimate_sigma_yx(self):
        """Update sigma_yx for each replicate.

        """

        squared_loss = np.array([
            torch.linalg.norm(torch.addmm(Y, dataset.obsm["X"], dataset.uns["M"][f"{replicate}"].T, alpha=-1), ord='fro').item() ** 2
            for Y, X, dataset, replicate in zip(self.Ys, self.Xs, self.datasets, self.repli_list)
        ])
        sizes = np.array([np.prod(dataset.X.shape) for dataset in self.datasets])
        if self.sigma_yx_inv_mode == 'separate':
            self.sigma_yxs = np.sqrt(squared_loss / sizes)
        elif self.sigma_yx_inv_mode == 'average':
            sigma_yx = np.sqrt(np.dot(self.betas, squared_loss) / np.dot(self.betas, sizes))
            self.sigma_yxs = np.full(self.num_repli, float(sigma_yx))
        else:
            raise NotImplementedError

        for dataset, sigma_yx in zip(self.datasets, self.sigma_yxs):
            dataset.uns["sigma_yx"] = sigma_yx

    def compute_likelihood_function(self, use_spatial):
        """Function to compute overall likelihood function P(Y, X, Theta).

        Everything is computed for negative of the log-likelihood, so at the end we compute $$e^{-\text{sum}}$$
        
        """

        log_likelihood = 0

        size_factors = [torch.linalg.norm(X, axis=1, ord=1, keepdim=True) for X in self.Xs ] # s_i
        Zs = [X / size_factor for X, size_factor in zip(self.Xs, size_factors)] # z_i
        weighted_total_cells = 0
        for dataset, is_spatial_fov in zip(self.datasets, use_spatial):
            if is_spatial_fov:
                adjacency_list = dataset.obs["adjacency_list"]
                weighted_total_cells += sum(map(len, adjacency_list)) # * beta

        for replicate, dataset, X, Z, is_spatial_fov in zip(self.repli_list, self.datasets, self.Xs,Zs, use_spatial):
            lambda_x = dataset.uns["spicemixplus_hyperparameters"]["prior_x"].detach().cpu().numpy()
            K = dataset.uns["spicemixplus_hyperparameters"]["K"]
            M = dataset.uns["M"][f"{replicate}"]
            M_bar = dataset.uns["M_bar"][f"{replicate}"]
            Y = torch.tensor(dataset.X, **self.context)
            N, G = Y.shape

            if is_spatial_fov:
                Sigma_x_inv = dataset.uns["Sigma_x_inv"][f"{replicate}"]
            sigma_yx = dataset.uns["sigma_yx"]
            adjacency_matrix = dataset.obsp["adjacency_matrix"]
            
            linear_term = 0
            if is_spatial_fov:
                linear_term_coefficient = torch.zeros_like(Sigma_x_inv).requires_grad_(False) # Initialized as zero to handle NMF case
            if is_spatial_fov:
                nu = adjacency_matrix @ Z
                linear_term_coefficient = Z.T @ nu # * beta
            else:
                nu = None

            # Compute loss 
            if is_spatial_fov:
                linear_term = Sigma_x_inv.view(-1) @ linear_term_coefficient.view(-1) # sum_(i, j) z_i^T @ Sigma_x_inv @ z_j
            
            gaussian_factor = np.log(np.pi / self.lambda_Sigma_x_inv)*((K**2)/2)
            Sigma_x_inv_prior_regularization = 0
            if is_spatial_fov:
                Sigma_x_inv_prior_regularization = weighted_total_cells * (gaussian_factor + self.lambda_Sigma_x_inv * Sigma_x_inv.pow(2).sum() / 2) # lambda_Sigma_x_inv * ||Sigma_x_inv||^2_F / 2

                if self.Sigma_x_inv_bar is not None:
                    Sigma_x_inv_bar = dataset.uns["Sigma_x_inv_bar"][f"{replicate}"]
                    Sigma_x_inv_prior_regularization += self.lambda_Sigma_x_inv * 100 * (self.Sigma_x_inv_bar - Sigma_x_inv).pow(2).sum() * weighted_total_cells / 2

            log_partition_function = 0
            logZ_i_Y = torch.full((N,), G/2 * np.log((2 * np.pi * sigma_yx**2)), **self.context)
            print(f"Lambda x:{lambda_x}")
            if nu is None:
                logZ_i_X = torch.full((N,), 0, **self.context)
                if lambda_x[0]:
                    logZ_i_X +=  torch.full((N,), K * np.log(lambda_x[0]), **self.context)
                
                # print(logZ_i_Y)
                # print(logZ_i_X)
                log_partition_function = (logZ_i_X + logZ_i_Y).sum() # * beta
            else:
                eta = nu @ Sigma_x_inv
                logZ_i_z = -integrate_of_exponential_over_simplex(eta)
                # print(logZ_i_z)
                logZ_i_s = torch.full((N,), 0, **self.context)
                if lambda_x[0]:
                    logZ_i_s = torch.full((N,), -K * np.log(lambda_x[0]) + np.log(factorial(K-1, exact=True)), **self.context)
                # print(logZ_i_s)

                log_partition_function = (logZ_i_z + logZ_i_s + logZ_i_Y).sum() # * beta

            print(log_partition_function)
            print(Sigma_x_inv_prior_regularization)
            log_likelihood += (linear_term + log_partition_function) + Sigma_x_inv_prior_regularization
           
            assert log_likelihood != torch.inf

            # phi = (Y - X @ M.T).T @ (Y - X @ M.T) / (2 * sigma_yx**2)
            phi = (torch.linalg.norm((Y- X @ M.T)) ** 2) / (2 * sigma_yx **2)

            print(phi)
            log_likelihood += phi
            assert log_likelihood != torch.inf

            # -lambda_M ||M_bar - M||^2_2
            if self.lambda_M > 0 and M_bar is not None:
                print("Differential loss")
                gaussian_factor = np.log(np.pi / self.lambda_M)*((G * K)/2)
                metagene_regularization = gaussian_factor + self.lambda_M * (M_bar - M).pow(2).sum() / 2

                print(metagene_regularization)
                log_likelihood += metagene_regularization
                assert log_likelihood != torch.inf
          
            x_regularization = torch.tensor(0)
            if lambda_x[-1]:
                x_regularization = -np.log(lambda_x[-1]) + lambda_x[-1] * torch.norm(X, p=1)
            print(x_regularization)
            log_likelihood += x_regularization
            
            # print(log_likelihood)
            assert log_likelihood != torch.inf

        return log_likelihood

    def estimate_weights(self, iiter, use_spatial, backend_algorithm="nesterov"):
        """Update weights (latent states) for each replicate.

        """

        logging.info(f'{print_datetime()}Updating latent states')
        assert len(use_spatial) == self.num_repli

        assert self.X_constraint == 'none'
        assert self.pairwise_potential_mode == 'normalized'

        loss_list = []
        for i, (X, sigma_yx, phenotype, prior_x_mode, prior_x, replicate, dataset, Y) in enumerate(zip(
                self.Xs, self.sigma_yxs, self.phenotypes, self.prior_x_modes, self.prior_xs, self.repli_list, self.datasets, self.Ys)):

            valid_keys = [k for k, v in phenotype.items() if v is not None]

            M = dataset.uns["M"][f"{replicate}"]

            if len(valid_keys) > 0:
                loss, self.Xs[i] = estimate_weight_wnbr_phenotype(
                    Y, M, X, sigma_yx, replicate, prior_x_mode, prior_x,
                    {k: phenotype[k] for k in valid_keys}, {k: self.phenotype_predictors[k] for k in valid_keys},
                    dataset, context=self.context,
                )
                dataset.obsm["X"] = self.Xs[i]
            elif not use_spatial[i]:
                loss, self.Xs[i] = estimate_weight_wonbr(
                    Y, M, X, sigma_yx, replicate, prior_x_mode, prior_x, dataset, context=self.context, update_alg=backend_algorithm)
                dataset.obsm["X"] = self.Xs[i]
            else:
                loss, self.Xs[i] = estimate_weight_wnbr(
                    Y, M, X, sigma_yx, replicate, prior_x_mode, prior_x, dataset, context=self.context, update_alg=backend_algorithm)
                dataset.obsm["X"] = self.Xs[i]

            loss_list.append(loss)

        return loss_list

    def estimate_phenotype_predictor(self):
        for phenotype_name, predictor_tuple in self.phenotype_predictors.items():
            input_target_list = []
            for X, phenotype, dataset in zip(self.Xs, self.phenotypes, self.datasets):
                if phenotype[phenotype_name] is None:
                    continue

                # Normalizing X per cell
                X = X / torch.linalg.norm(X, ord=1, dim=1, keepdim=True)
                input_target_list.append((X, phenotype[phenotype_name]))
            estimate_phenotype_predictor(*zip(*input_target_list), phenotype_name, *predictor_tuple)

    def estimate_M(self):
        betas = self.betas / np.sum(self.betas) # not sure if we should do this
        if self.metagene_mode == "shared":
            # Since the metagenes are shared, we can just use the version stored in the first replicate
            first_replicate = self.repli_list[0]
            first_dataset = self.datasets[0]
            M = estimate_M(self.Xs, first_dataset.uns["M"][f"{first_replicate}"], self.sigma_yxs, betas, self.datasets, M_bar=self.M_bar,
                lambda_M=self.lambda_M, M_constraint=self.M_constraint, context=self.context)

            for dataset, replicate in zip(self.datasets, self.repli_list):
                dataset.uns["M"][f"{replicate}"] = M

        elif self.metagene_mode == "differential":
            for index, (dataset, replicate) in enumerate(zip(self.datasets, self.repli_list)):
                dataset.uns["M"][f"{replicate}"] = estimate_M(self.Xs[index:index+1], dataset.uns["M"][f"{replicate}"], self.sigma_yxs[index:index+1], [1],
                    self.datasets[index:index+1], M_bar=self.M_bar, lambda_M=self.lambda_M, M_constraint=self.M_constraint, context=self.context)

        # Set M_bar to average of self.Ms
        self.M_bar.zero_()
        # for group, M in self.Ms.items():
        #     self.M_bar.add_(M)
        for dataset, replicate in zip(self.datasets, self.repli_list):
            self.M_bar.add_(dataset.uns["M"][f"{replicate}"])
        self.M_bar.div_(len(self.datasets))

        for dataset, replicate in zip(self.datasets, self.repli_list):
            dataset.uns["M_bar"][f"{replicate}"] = self.M_bar

    def estimate_parameters(self, iiter, use_spatial, update_Sigma_x_inv=True):
        logging.info(f'{print_datetime()}Updating model parameters')

        if update_Sigma_x_inv and np.any(use_spatial):
            if self.Sigma_x_inv_mode == "shared":
                updated_Sigma_x_inv, Q_value = estimate_Sigma_x_inv(
                    self.Xs, self.Sigma_x_inv, use_spatial, self.lambda_Sigma_x_inv,
                    self.betas, self.optimizer_Sigma_x_inv, self.context, self.datasets)
                with torch.no_grad():
                    # Note: in-place update is necessary here in order for optimizer to track same object
                    self.Sigma_x_inv[:] = updated_Sigma_x_inv
                    for replicate, dataset in zip(self.repli_list, self.datasets):
                        dataset.uns["Sigma_x_inv"][f"{replicate}"][:] = updated_Sigma_x_inv

                # self.optimizer_Sigma_x_inv.param_groups[0]["params"] = self.Sigma_x_inv
            elif self.Sigma_x_inv_mode == "differential":
                for X, dataset, u, beta, optimizer, replicate in zip(self.Xs, self.datasets, use_spatial,
                        self.betas, self.optimizer_Sigma_x_invs, self.repli_list):
                    updated_Sigma_x_inv, Q_value = estimate_Sigma_x_inv([X], dataset.uns["Sigma_x_inv"][f"{replicate}"],
                            [u], self.lambda_Sigma_x_inv, [beta], optimizer, self.context, [dataset])
                    with torch.no_grad():
                        # Note: in-place update is necessary here in order for optimizer to track same object
                        dataset.uns["Sigma_x_inv"][f"{replicate}"][:] = updated_Sigma_x_inv

                    print(f"Q_value:{Q_value}")
       
                self.Sigma_x_inv_bar.zero_()
                for dataset, replicate in zip(self.datasets, self.repli_list):
                    self.Sigma_x_inv_bar.add_(dataset.uns["Sigma_x_inv"][f"{replicate}"])
                
                self.Sigma_x_inv_bar.div_(len(self.datasets))

                for dataset, replicate in zip(self.datasets, self.repli_list):
                    dataset.uns["Sigma_x_inv_bar"][f"{replicate}"] = self.Sigma_x_inv_bar

        self.estimate_M()
        self.estimate_sigma_yx()
        self.estimate_phenotype_predictor()

        # self.save_parameters(iiter=iiter)

    def save_results(self, path2dataset, iteration, PredictorConstructor=None, predictor_hyperparams=None, filename=None):
        if not filename:
            filename = f"trained_iteration_{iteration}.h5"

        save_anndata(path2dataset / filename, self.datasets, self.repli_list)
        if self.phenotype_predictors:
            save_predictors(path2dataset, self.phenotype_predictors, iteration) 
