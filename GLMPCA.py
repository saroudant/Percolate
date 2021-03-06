import numpy as np
import pandas as pd
import torch, os
import torch.optim
import matplotlib.pyplot as plt
from copy import deepcopy
from joblib import Parallel, delayed
import mctorch.nn as mnn
from pickle import dump, load
import mctorch.optim as moptim
from torch.utils.data import Dataset, TensorDataset, DataLoader
from scipy.stats import beta as beta_dst
from scipy.stats import lognorm
from scipy.stats import gamma as gamma_dst

from .negative_binomial_routines import compute_dispersion
from .exponential_family import *
from .log_normal import LOG_NORMAL_ZERO_THRESHOLD

LEARNING_RATE_LIMIT = 10**(-20)


def _create_saturated_loading_optim(
    parameters, data, n_pc, family, 
    learning_rate, max_value=np.inf, exp_family_params=None, step_size=20, gamma=0.5
    ):
    
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # Initialize loadings with spectrum
    _,_,v = torch.linalg.svd(parameters - torch.mean(parameters, axis=0))
    loadings = mnn.Parameter(
        data=v[:n_pc,:].T.to(device),
        manifold=mnn.Stiefel(parameters.shape[1], n_pc)
    )
    # intercept = mnn.Parameter(
    #     data=torch.mean(parameters, axis=0),
    #     manifold=mnn.Euclidean(parameters.shape[1])
    # )
    intercept = torch.mean(parameters, axis=0).to(device)
    
    params = deepcopy(exp_family_params)
    # Load to GPU
    if params is not None:
        params = {
            k:params[k].to(device) if type(params[k]) is torch.Tensor else params[k]
            for k in params
        }

    if family.lower() in ['negative_binomial', 'nb', 'negative_binomial_reparam', 'nb_rep']:
        params['r'] = params['r'][params['gene_filter']]
    cost = make_saturated_loading_cost(
        family=family,
        max_value=max_value, 
        params=params,
        train=True
    )
    # optimizer = moptim.ConjugateGradient(params = [loadings, intercept], lr=learning_rate)
    optimizer = moptim.rAdagrad(params = [loadings, intercept], lr=learning_rate)
    optimizer = moptim.rAdagrad(params = [loadings], lr=learning_rate)
    # optimizer = moptim.rASA(params=[loadings, intercept], lr=learning_rate)
    # optimizer = moptim.rASA(params=[loadings], lr=learning_rate)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=step_size, gamma=gamma)

    return optimizer, cost, loadings, intercept, lr_scheduler


def _create_saturated_scores_projection_optim(parameters, data, n_pc, family, learning_rate, max_value=np.inf):
    scores = mnn.Parameter(manifold=mnn.Stiefel(parameters.shape[0], n_pc))
    cost = make_saturated_sample_proj_cost(family, parameters, data, max_value)
    # optimizer = moptim.ConjugateGradient(params=[scores], lr=learning_rate)
    optimizer = moptim.rAdagrad(params=[scores], lr=learning_rate)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=20, gamma=0.5)

    return optimizer, cost, scores, lr_scheduler


class GLMPCA:

    def __init__(
        self, 
        n_pc, 
        family, 
        maxiter=1000, 
        max_param = 10,
        learning_rate = 0.02,
        batch_size=128,
        step_size=20,
        gamma=0.5,
        n_init=1,
        n_jobs=1
        ):

        self.n_pc = n_pc
        self.family = family
        self.maxiter = maxiter
        self.log_part_theta_matrices_ = None
        self.max_param = np.abs(max_param)
        self.learning_rate_ = learning_rate
        self.initial_learning_rate_ = learning_rate
        self.n_jobs = n_jobs
        self.batch_size = batch_size
        self.n_init = n_init
        self.gamma = gamma
        self.step_size = step_size

        self.saturated_loadings_ = None
        # saturated_intercept_: before projecting
        self.saturated_intercept_ = None
        # reconstruction_intercept: after projecting
        self.reconstruction_intercept_ = None

        # Whether to perform sample or gene projection
        self.sample_projection = False

        self.exp_family_params = None
        self.loadings_learning_scores_ = []
        self.loadings_learning_rates_ = []


    def compute_saturated_loadings(self, X, exp_family_params=None, batch_size=None, n_init=None, saturated_params=None):
        """
        Compute low-rank feature-level projection of saturated parameters.
        """
        # Set device
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.batch_size = batch_size if batch_size is not None else self.batch_size
        self.n_init = n_init if n_init is not None else self.n_init
        
        if exp_family_params is not None:
            self.exp_family_params = exp_family_params

        if saturated_params is None:
            self.saturated_param_ = self.compute_saturated_params(
                X, 
                with_intercept=False, 
                exp_family_params=self.exp_family_params, 
                save_family_params=True
            ).to(device)
        else:
            self.saturated_param_ = saturated_params

        self.learning_rate_ = self.initial_learning_rate_
        self.loadings_learning_scores_ = []
        self.loadings_learning_rates_ = []

        if self.n_init == 1:
            self.saturated_loadings_, self.saturated_intercept_ = self._saturated_loading_iter(
                self.saturated_param_, 
                X[:,self.exp_family_params['gene_filter']] if self.family.lower() in ['negative_binomial', 'nb', 'negative_binomial_reparam', 'nb_rep'] else X,
                batch_size=self.batch_size
            )
        else:
            # Perform several initializations and select the top ones.
            init_results = [
                self._saturated_loading_iter(
                    self.saturated_param_, 
                    X[:,self.exp_family_params['gene_filter']] if self.family.lower() in ['negative_binomial', 'nb', 'negative_binomial_reparam', 'nb_rep'] else X,
                    batch_size=self.batch_size,
                    return_train_likelihood=True
                ) for _ in range(self.n_init)
            ]
            self.iter_likelihood_results_ = [e[-1].cpu().detach().numpy() for e in init_results]
            self.optimal_iter_arg_ = np.argmin(self.iter_likelihood_results_)
            self.saturated_loadings_, self.saturated_intercept_ = init_results[self.optimal_iter_arg_][:2]


        self.saturated_intercept_ = self.saturated_intercept_.clone().detach().to(device)
        self.reconstruction_intercept_ = self.saturated_intercept_.clone().detach().to(device)
        self.saturated_param_ = self.saturated_param_ - self.saturated_intercept_
        self.sample_projection = False

        return self.saturated_loadings_


    def compute_saturated_orthogonal_scores(self, X=None, correct_loadings=False):
        """
        Compute low-rank sample-level orthogonal projection of saturated parameters.
        If correct_loadings, align loadings to have perfect match with scores
        """
        if self.saturated_loadings_ is None:
            self.compute_saturated_loadings(X)

        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        if X is not None:
            self.saturated_param_ = self.compute_saturated_params(
                X, 
                with_intercept=True, 
                exp_family_params=self.exp_family_params,
                save_family_params=False
            ).to(device)

        projected_orthogonal_scores_ = self.saturated_param_.matmul(self.saturated_loadings_).matmul(self.saturated_loadings_.T)

        self.projected_orthogonal_scores_svd_ = torch.linalg.svd(projected_orthogonal_scores_, full_matrices=False)
        # Restrict to top components and return SVD in form U@S@V^T
        svd_results_ = []
        svd_results_.append(self.projected_orthogonal_scores_svd_[0][:,:self.n_pc])
        svd_results_.append(self.projected_orthogonal_scores_svd_[1][:self.n_pc])
        svd_results_.append(self.projected_orthogonal_scores_svd_[2].T[:,:self.n_pc])
        self.projected_orthogonal_scores_svd_ = svd_results_

        # Compute saturated scores by taking the left singular values
        self.saturated_scores_ = self.projected_orthogonal_scores_svd_[0]

        if correct_loadings:
            self.saturated_loadings_ = torch.matmul(self.saturated_loadings_, self.projected_orthogonal_scores_svd_[2].T)
            # self.saturated_loadings_weights_ = 1./self.projected_orthogonal_scores_svd_[1]
            # It is Sigma_A (or Sigma_B) in the derivation
            self.saturated_loadings_weights_ = self.projected_orthogonal_scores_svd_[1]
            self.saturated_loadings_ = torch.matmul(self.saturated_loadings_, torch.diag(1./self.projected_orthogonal_scores_svd_[1]))
            self.sample_projection = True

        return self.saturated_scores_


    def compute_reconstructed_data(self, X, scores):
        """
        Given some orthogonal scores, compute the expected data.
        """
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        saturated_param_ = self.compute_saturated_params(
            X.cpu(), with_intercept=False, exp_family_params=self.exp_family_params
        )

        # Compute associated cell view
        joint_saturated_param_ = deepcopy(saturated_param_.detach())
        if self.saturated_intercept_ is not None:
            joint_saturated_param_ = joint_saturated_param_ - self.saturated_intercept_
        joint_saturated_param_ = torch.matmul(scores, scores.T).matmul(joint_saturated_param_)
        if self.saturated_intercept_ is not None:
            joint_saturated_param_ = joint_saturated_param_ + self.reconstruction_intercept_

        params = deepcopy(self.exp_family_params)
        if self.family.lower() in ['negative_binomial', 'nb', 'negative_binomial_reparam', 'nb_rep']:
            params['r'] = params['r'][params['gene_filter']]
        self.X_reconstruct_view_ = G_grad_fun(self.family)(joint_saturated_param_, params)

        return self.X_reconstruct_view_


    def compute_projected_saturated_params(self, X, with_reconstruction_intercept=True, exp_family_params=None):
        # Compute saturated params
        saturated_param_ = self.compute_saturated_params(
            X, 
            with_intercept=True, 
            exp_family_params=exp_family_params if exp_family_params is not None else self.exp_family_params, 
            save_family_params=False
        )

        # Project on loadings
        saturated_param_ = torch.matmul(saturated_param_, self.saturated_loadings_)
        saturated_param_ = torch.matmul(saturated_param_, torch.linalg.pinv(self.saturated_loadings_))
        if with_reconstruction_intercept:
            saturated_param_ = saturated_param_ + self.reconstruction_intercept_

        return saturated_param_.detach()


    def compute_saturated_params(self, X, with_intercept=True, exp_family_params=None, save_family_params=False, n_jobs=None):
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        n_jobs = self.n_jobs if n_jobs is None else n_jobs

        if self.family.lower() in ['negative_binomial', 'nb', 'negative_binomial_reparam', 'nb_rep']:
            # Load parameter if needed
            if exp_family_params is not None and 'r' in exp_family_params:
                r_coef = exp_family_params['r'].clone()
                gene_filter = exp_family_params['gene_filter'].clone()
            else:
                r_coef = 1. / torch.Tensor(compute_dispersion(pd.DataFrame(X.detach().numpy())).values).flatten()
                gene_filter = torch.where((r_coef > 0.01) & (~torch.isnan(r_coef)) & (r_coef < 10**4))[0]

            # Save parameters if required
            if save_family_params:
                if self.exp_family_params is None:
                    self.exp_family_params = {}
                self.exp_family_params['r'] = torch.Tensor(r_coef)
                self.exp_family_params['gene_filter'] = torch.where((r_coef > 0.01) & (~torch.isnan(r_coef)) & (r_coef < 10**4))[0]

            # Filter genes
            r_coef = r_coef[gene_filter]
            X_data = X[:,gene_filter]
            # exp_family_params = {'r': r_coef, 'gene_filter': gene_filter}
            saturated_param_ = g_invertfun(self.family)(X_data.to(device), self.exp_family_params_gpu())

        elif self.family.lower() in ['beta_reparam', 'beta_rep']:
            if exp_family_params is not None and 'nu' in exp_family_params:
                nu = exp_family_params['nu'].clone()
            else:
                beta_parameters = [
                    beta_dst.fit(X_feat, floc=0, fscale=1)
                    for X_feat in X.T
                ]
                nu = torch.Tensor([e[0] + e[1] for e in beta_parameters])
            
            if save_family_params:
                if self.exp_family_params is None:
                    self.exp_family_params = {}
                self.exp_family_params['nu'] = nu
                self.exp_family_params['n_jobs'] = n_jobs

            if 'n_jobs' not in self.exp_family_params:
                self.exp_family_params['n_jobs'] = n_jobs

            saturated_param_ = g_invertfun(self.family)(X.cpu(), self.exp_family_params_cpu())

        elif self.family.lower() in ['beta']:
            if exp_family_params is not None and 'beta' in exp_family_params:
                beta_parameters = exp_family_params['beta'].clone()
            else:
                beta_parameters = [
                    beta_dst.fit(X_feat, floc=0, fscale=1)[1]
                    for X_feat in X.T
                ]
            
            if save_family_params:
                if self.exp_family_params is None:
                    self.exp_family_params = {}
                self.exp_family_params['beta'] = torch.Tensor(beta_parameters)
                self.exp_family_params['n_jobs'] = n_jobs

            if 'n_jobs' not in self.exp_family_params:
                self.exp_family_params['n_jobs'] = n_jobs
            saturated_param_ = g_invertfun(self.family)(X.cpu(), self.exp_family_params_cpu())

        elif self.family.lower() in ['log_normal', 'log normal', 'lognorm']:
            if exp_family_params is not None and 'nu' in exp_family_params:
                nu_parameters = exp_family_params['nu'].clone()
            else:
                nu_parameters = Parallel(n_jobs=n_jobs, verbose=1)(
                    delayed(lognorm.fit)(X_feat, loc=0) for X_feat in X.T
                )
                nu_parameters = torch.Tensor([e[0] for e in nu_parameters])

            if save_family_params:
                if self.exp_family_params is None:
                    self.exp_family_params = {}
                self.exp_family_params['nu'] = torch.Tensor(nu_parameters)

            saturated_param_ = g_invertfun(self.family)(X.cpu(), self.exp_family_params_cpu())

        elif self.family.lower() in ['gamma']:
            if exp_family_params is not None and 'nu' in exp_family_params:
                nu_parameters = exp_family_params['nu'].clone()
            else:
                nu_parameters = Parallel(n_jobs=n_jobs, verbose=1)(
                    delayed(gamma_dst.fit)(X_feat, loc=0) for X_feat in X.T
                )
                nu_parameters = torch.Tensor([1/e[2] for e in nu_parameters])

            if save_family_params:
                if self.exp_family_params is None:
                    self.exp_family_params = {}
                self.exp_family_params['nu'] = torch.Tensor(nu_parameters)
                self.exp_family_params['n_jobs'] = n_jobs

            saturated_param_ = g_invertfun(self.family)(X.cpu(), self.exp_family_params_cpu())

        else:
            # Compute saturated params
            if save_family_params and self.exp_family_params is None:
                self.exp_family_params = {}
            saturated_param_ = g_invertfun(self.family)(X.to(device), self.exp_family_params_gpu())

        saturated_param_ = torch.clip(saturated_param_, -self.max_param, self.max_param)

        # Project on loadings
        if with_intercept:
            saturated_param_ = saturated_param_.to(device) - self.saturated_intercept_.to(device)

        return saturated_param_.clone().detach().to(device)


    def project_low_rank(self, X):
        saturated_params = self.compute_saturated_params(
            X, 
            with_intercept=True,
            exp_family_params=self.exp_family_params
        )
        return saturated_params.matmul(self.saturated_loadings_)


    def project_low_rank_from_saturated_parameters(self, saturated_params):
        return saturated_params.matmul(self.saturated_loadings_)


    def project_cell_view(self, X):
        projected_saturated_param_ = self.compute_projected_saturated_params(
            X, 
            with_reconstruction_intercept=True,
            exp_family_params=self.exp_family_params
        )


        return G_grad_fun(self.family)(projected_saturated_param_, self.exp_family_params_gpu)


    def clone_empty_GLMPCA(self):
        glmpca_clf = GLMPCA( 
            self.n_pc, 
            self.family, 
            maxiter=self.maxiter, 
            max_param=self.max_param,
            learning_rate=self.learning_rate_,
            n_jobs=self.n_jobs,
            gamma=self.gamma,
            step_size=self.step_size
        )
        glmpca_clf.saturated_intercept_ = self.saturated_intercept_.clone().detach()
        glmpca_clf.reconstruction_intercept_ = self.reconstruction_intercept_.clone().detach()
        glmpca_clf.exp_family_params = self.exp_family_params

        return glmpca_clf


    def _saturated_loading_iter(self, saturated_param, data, batch_size=128, return_train_likelihood=False):
        """
        Computes the loadings, i.e. orthogonal low-rank projection, which maximise the likelihood of the data.
        """

        if self.learning_rate_ < LEARNING_RATE_LIMIT:
            raise ValueError('LEARNING RATE IS TOO SMALL : DID NOT CONVERGE')

        # Set device for GPU usage
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        print('SATURATED LOADINGS: USING DEVICE %s'%(device))

        _optimizer, _cost, _loadings, _intercept, _lr_scheduler = _create_saturated_loading_optim(
            saturated_param.data.clone(),
            data,
            self.n_pc,
            self.family,
            self.learning_rate_,
            self.max_param,
            self.exp_family_params,
            gamma=self.gamma,
            step_size=self.step_size
        )

        _loadings = _loadings.to(device)
        _intercept = _intercept.to(device)
        self.loadings_elements_optim_ = [_optimizer, _cost, _loadings, _intercept, _lr_scheduler]
        
        self.loadings_learning_scores_.append([])
        self.loadings_learning_rates_.append([])
        previous_loadings = _loadings.clone()
        previous_intercept = _intercept.clone()

        data = data.to(device)
        saturated_param = saturated_param.to(device)
        train_data = TensorDataset(data, saturated_param.data.clone())
        train_loader = DataLoader(dataset=train_data, batch_size=batch_size, shuffle=True)
        # train_loader = train_loader.to(device)

        for idx in range(self.maxiter):
            if idx % 100 == 0:
                print('\tSTART ITER %s'%(idx))
            loss_val = []
            for data_batch, param_batch in train_loader:
                cost_step = _cost(
                    X=_loadings, 
                    data=data_batch, 
                    parameters=param_batch, 
                    intercept=_intercept
                )

                if 'cuda' in str(device) :
                    self.loadings_learning_scores_[-1].append(cost_step.cpu().detach().numpy())
                else:
                    self.loadings_learning_scores_[-1].append(cost_step.detach().numpy())
                cost_step.backward()
                _optimizer.step()
                _optimizer.zero_grad()
                self.loadings_learning_rates_[-1].append(_lr_scheduler.get_last_lr())
            _lr_scheduler.step()

            if np.isinf(self.loadings_learning_scores_[-1][-1]) or np.isnan(self.loadings_learning_scores_[-1][-1]):
                print('\tRESTART BECAUSE INF/NAN FOUND', flush=True)
                self.learning_rate_ = self.learning_rate_ * self.gamma
                self.loadings_learning_scores_ = self.loadings_learning_scores_[:-1]
                self.loadings_learning_rates_ = self.loadings_learning_rates_[:-1]

                # Remove memory
                del train_data, train_loader, _optimizer, _cost, _loadings, _intercept, _lr_scheduler, self.loadings_elements_optim_
                if 'cuda' in str(device):
                    torch.cuda.empty_cache()

                return self._saturated_loading_iter(
                    saturated_param=saturated_param,
                    data=data, 
                    batch_size=batch_size, 
                    return_train_likelihood=return_train_likelihood
                )

        print('\tEND OPTIMISATION\n')

        if return_train_likelihood:
            params = {
                k: self.exp_family_params[k].to(device) if type(self.exp_family_params[k]) is torch.Tensor else self.exp_family_params[k]
                for k in self.exp_family_params
            }
            if params is not None and self.family.lower() in ['negative_binomial', 'nb', 'negative_binomial_reparam', 'nb_rep']:
                params['r'] = params['r'][self.exp_family_params['gene_filter']].to(device)

            projected_likelihood = make_saturated_loading_cost(
                self.family, max_value=self.max_param, params=params, train=False
            )
            _likelihood = projected_likelihood(
                _loadings,
                data,
                saturated_param,
                _intercept
            )

            return _loadings, _intercept, _likelihood

        # Reinitialize learning rate
        self.learning_rate_ = deepcopy(self.initial_learning_rate_)
        return _loadings, _intercept


    def exp_family_params_cpu(self):
        if self.exp_family_params is None:
            return None
        return {
            k: self.exp_family_params[k].cpu() if type(self.exp_family_params[k]) is torch.Tensor() else self.exp_family_params[k]
            for k in self.exp_family_params
        }

    def exp_family_params_gpu(self):
        if self.exp_family_params is None:
            return None
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        return {
            k: self.exp_family_params[k].to(device) if type(self.exp_family_params[k]) is torch.Tensor() else self.exp_family_params[k]
            for k in self.exp_family_params
        }


    def save(self, folder):

        if not os.path.isdir(folder):
            os.mkdir(folder)
        else:
            print('WARNING, folder already existing. This may crush pre-existing results', flush=True)

        parameters = {
            'n_pc': self.n_pc,
            'family': self.family,
            'maxiter': self.maxiter,
            'log_part_theta_matrices_': self.log_part_theta_matrices_,
            'max_param': self.max_param,
            'learning_rate_': self.learning_rate_,
            'initial_learning_rate_': self.initial_learning_rate_
        }
        dump(parameters, open('%s/params.pkl'%(folder), 'wb'))
        
        if hasattr(self, 'saturated_loadings_') and self.saturated_loadings_ is not None:
            torch.save(self.saturated_loadings_.cpu(), '%s/saturated_loadings_.pt'%(folder))
        if hasattr(self, 'saturated_intercept_') and self.saturated_intercept_ is not None:
            torch.save(self.saturated_intercept_.cpu(), '%s/saturated_intercept_.pt'%(folder))
        if hasattr(self, 'saturated_scores_') and self.saturated_scores_ is not None:
            torch.save(self.saturated_scores_.cpu(), '%s/saturated_scores_.pt'%(folder))
        if hasattr(self, 'exp_family_params') and self.exp_family_params is not None:
            dump(self.exp_family_params, open('%s/exp_family_params.pkl'%(folder), 'wb'))
        if hasattr(self, 'projected_orthogonal_scores_svd_') and self.projected_orthogonal_scores_svd_ is not None:
            dump(
                [x.cpu() for x in self.projected_orthogonal_scores_svd_], 
                open('%s/projected_orthogonal_scores_svd.pkl'%(folder), 'wb')
            )

        return True


    def load(folder, device='cpu'):
        instance = GLMPCA(n_pc=-1, family='NA', maxiter=-1, max_param=-1, learning_rate=-1)

        # Import parameters
        parameters = load(open('%s/params.pkl'%(folder), 'rb'))
        instance.n_pc = parameters['n_pc']
        instance.family = parameters['family']
        instance.maxiter = parameters['maxiter']
        instance.log_part_theta_matrices_ = parameters['log_part_theta_matrices_']
        instance.max_param = parameters['max_param']
        instance.learning_rate_ = parameters['learning_rate_']
        instance.initial_learning_rate_ = parameters['initial_learning_rate_']

        # Import computed loadings
        device = torch.device('cpu') if device is None else torch.device(device)
        if 'saturated_loadings_.pt' in os.listdir(folder):
            instance.saturated_loadings_ = torch.load('%s/saturated_loadings_.pt'%(folder), map_location=device)
        if 'saturated_intercept_.pt' in os.listdir(folder):
            instance.saturated_intercept_ = torch.load('%s/saturated_intercept_.pt'%(folder), map_location=device)
            instance.reconstruction_intercept_ = torch.load('%s/saturated_intercept_.pt'%(folder), map_location=device)
        if 'saturated_scores_.pt' in os.listdir(folder):
            instance.saturated_scores_ = torch.load('%s/saturated_scores_.pt'%(folder), map_location=device)
        if 'exp_family_params.pkl' in os.listdir(folder):
            instance.exp_family_params = load(open('%s/exp_family_params.pkl'%(folder), 'rb'))
        if 'projected_orthogonal_scores_svd.pkl' in os.listdir(folder):
            instance.projected_orthogonal_scores_svd_ = load(open('%s/projected_orthogonal_scores_svd.pkl'%(folder), 'rb'))

        return instance

        
        