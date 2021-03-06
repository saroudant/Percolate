""" Exponential family using tensor structure

@author: Soufiane Mourragui <soufiane.mourragui@gmail.com>

Codes supporting routines involving the Exponential Family, e.g. Maximum Likelihood
Estimation (MLE) in glm-PCA.

We assume that MLE writes like:
'''
\sum_{i=1}^n G(\theta_{i,j}) - \Theta^T T(x)

for j \in \{1, .., p\} feature index. T(x) is xx^T for most applications we use here.
'''

Example
-------

    
Notes
-------


References
-------


"""

import torch
from copy import deepcopy
import numpy as np
import scipy
from sklearn.preprocessing import StandardScaler
from joblib import Parallel, delayed

from .beta_routines import compute_alpha, compute_alpha_gene, compute_mu_gene
from.log_normal import LOG_NORMAL_ZERO_THRESHOLD, pi_val
from .gamma import GAMMA_ZERO_THRESHOLD, compute_gamma_saturated_params_gene

saturation_eps = 10**-10


# Compute the product of natural parameter and data (exp term)
def expt_gaussian(data, saturated_params, params=None):
    return torch.multiply(data, saturated_params)

def expt_bernoulli(data, saturated_params, params=None):
    return torch.multiply(data, saturated_params)

def expt_continuous_bernoulli(data, saturated_params, params=None):
    return torch.multiply(data, saturated_params)

def expt_poisson(data, saturated_params, params=None):
    return torch.multiply(data, saturated_params)

def expt_multinomial(data, saturated_params, params=None):
    return torch.multiply(data, saturated_params)

def expt_negative_binomial(data, saturated_params, params=None):
    return torch.multiply(data, saturated_params)

def expt_negative_binomial_reparametrized(data, saturated_params, params=None):
    # r = params['r']
    # reparam_saturated_params = - torch.log(1 + r * torch.exp(-saturated_params))
    # return torch.multiply(data, reparam_saturated_params)

    r = params['r']
    reparam_saturated_params = torch.log(r + torch.exp(saturated_params))

    # Correct difference
    reparam_saturated_params_isinf = torch.isinf(reparam_saturated_params)
    reparam_saturated_params[reparam_saturated_params_isinf] = saturated_params[reparam_saturated_params_isinf]
    reparam_saturated_params = torch.log(r) - reparam_saturated_params

    return torch.multiply(data, reparam_saturated_params)

def expt_beta_reparametrized(data, saturated_params, params=None):
    nu = params['nu']
    first_term = torch.multiply(torch.log(data), saturated_params * nu)
    second_term = torch.multiply(torch.log(1-data), (1-saturated_params) * nu)
    return first_term + second_term

def expt_beta(data, saturated_params, params=None):
    raise NotImplementedError

def expt_log_normal(data, saturated_params, params=None):
    nu = params['nu']
    first_term = saturated_params / (torch.pow(nu,2)) * torch.log(data + LOG_NORMAL_ZERO_THRESHOLD)
    second_term = - torch.pow(torch.log(data+LOG_NORMAL_ZERO_THRESHOLD), 2) / (2 * torch.pow(nu,2))
    return first_term + second_term

def expt_gamma(data, saturated_params, params=None):
    nu = params['nu']
    first_term = torch.log(data+GAMMA_ZERO_THRESHOLD) * saturated_params
    second_term = - data * nu
    return first_term + second_term

def expt_term(family):
    if family == 'bernoulli':
        return expt_bernoulli
    elif family == 'continuous_bernoulli':
        return expt_continuous_bernoulli
    elif family == 'poisson':
        return expt_poisson
    elif family == 'gaussian':
        return expt_gaussian
    elif family == 'multinomial':
        return expt_multinomial
    elif family.lower() in ['negative_binomial', 'nb']:
        return expt_negative_binomial
    elif family.lower() in ['negative_binomial_reparam', 'nb_rep']:
        return expt_negative_binomial_reparametrized
    elif family.lower() in ['beta']:
        return expt_beta
    elif family.lower() in ['beta_reparam', 'beta_rep']:
        return expt_beta_reparametrized
    elif family.lower() in ['log_normal', 'log normal', 'lognorm']:
        return expt_log_normal
    elif family.lower() in ['gamma']:
        return expt_gamma


# Functions G
# Corresponds to function A in manuscript
# x in the saturated parameters
def G_gaussian(x, params=None):
    return torch.square(x) / 2

def G_bernoulli(x, params=None):
    return torch.log(1+torch.exp(x))

def G_continuous_bernoulli(x, params=None):
    return torch.log((torch.exp(x)-1)/x)

def G_poisson(x, params=None):
    return torch.exp(x)

def G_multinomial(x, params=None):
    return 0

def G_negative_binomial(x, params=None):
    r = params['r']
    # the saturated parameters need to be negative
    if r.shape[0] != x.shape[1]:
        r = r[params['gene_filter']]
    return - r * torch.log(1-torch.exp(x.clip(-np.inf,-1e-7)))
    # return - r * torch.log(1-torch.exp(x))

def G_negative_binomial_reparametrized(x, params=None):
    r = params['r']
    if r.shape[0] != x.shape[1]:
        r = r[params['gene_filter']]
    # the saturated parameters need to be negative
    return r * torch.log(1 + r*torch.exp(-x))

def G_beta(x, params=None):
    beta = params['beta']
    return torch.lgamma(x) + torch.lgamma(beta) - torch.lgamma(x+beta)

def G_beta_reparametrized(x, params=None):
    nu = params['nu']
    return torch.lgamma(x * nu) + torch.lgamma((1-x)*nu) - torch.lgamma(nu)

def G_log_normal(x, params=None):
    nu = params['nu']
    return torch.pow(x/nu,2) / 2 + torch.log(nu)

def G_gamma(x, params=None):
    nu = params['nu']
    return torch.lgamma(x+1) - (x+1) * torch.log(nu)

def G_fun(family):
    if family == 'bernoulli':
        return G_bernoulli
    elif family == 'continuous_bernoulli':
        return G_continuous_bernoulli
    elif family == 'poisson':
        return G_poisson
    elif family == 'gaussian':
        return G_gaussian
    elif family == 'multinomial':
        return G_multinomial
    elif family.lower() in ['negative_binomial', 'nb']:
        return G_negative_binomial
    elif family.lower() in ['negative_binomial_reparam', 'nb_rep']:
        return G_negative_binomial_reparametrized
    elif family.lower() in ['beta']:
        return G_beta
    elif family.lower() in ['beta_reparam', 'beta_rep']:
        return G_beta_reparametrized
    elif family.lower() in ['log_normal', 'log normal', 'lognorm']:
        return G_log_normal
    elif family.lower() in ['gamma']:
        return G_gamma

# Functions G
# Corresponds to gradient of G, equals to the expectation of T
# 
def G_grad_gaussian(eta, params=None):
    return eta

def G_grad_bernoulli(eta, params=None):
    return 1./(1.+torch.exp(-eta))

def G_grad_continuous_bernoulli(eta, params=None):
    return torch.exp(eta) / (torch.exp(eta) - 1) - 1 / eta
    # return (torch.exp(-eta)+eta-1)/(eta * (1-torch.exp(-eta)))

def G_grad_poisson(eta, params=None):
    return torch.exp(eta)

def G_grad_multinomial(eta, params=None):
    return 0

def G_grad_negative_binomial(eta, params=None):
    r = params['r']
    if r.shape[0] != eta.shape[1]:
        r = r[params['gene_filter']]
    return r / (torch.exp(-eta) - 1)

def G_grad_negative_binomial_reparametrized(eta, params=None):
    r = params['r']
    if r.shape[0] != eta.shape[1]:
        r = r[params['gene_filter']]
    return r * r * torch.exp(-eta)

def G_grad_beta(eta, params=None):
    beta = params['b']
    return torch.digamma(eta) - torch.digamma(eta+beta)

def G_grad_beta_reparametrized(eta, params=None):
    nu = params['nu']
    return torch.digamma(eta * nu) - torch.digamma(nu)

def G_grad_log_norm(eta, params=None):
    nu = params['nu']
    return eta / (nu**2)

def G_grad_gamma(eta, params=None):
    nu = params['nu']
    return - (eta+1) / nu

def G_grad_fun(family):
    if family == 'bernoulli':
        return G_grad_bernoulli
    elif family == 'continuous_bernoulli':
        return G_grad_continuous_bernoulli
    elif family == 'poisson':
        return G_grad_poisson
    elif family == 'gaussian':
        return G_grad_gaussian
    elif family == 'multinomial':
        raise NotImplementedError('multinomial not implemented')
    elif family.lower() in ['negative_binomial', 'nb']:
        return G_grad_negative_binomial
    elif family.lower() in ['negative_binomial_reparam', 'nb_rep']:
        return G_grad_negative_binomial_reparametrized
    elif family.lower() in ['beta']:
        return G_grad_beta
    elif family.lower() in ['beta_reparam', 'beta_rep']:
        return G_grad_beta_reparametrized
    elif family.lower() in ['log_normal', 'log normal', 'lognorm']:
        return G_grad_log_norm
    elif family.lower() in ['gamma']:
        return G_grad_gamma

# g_invert is the inverse of the derivative of A.
def g_invert_bernoulli(x, params=None):
    y = deepcopy(x)
    y = torch.log(y/(1-y))
    return y
    # return - np_ag.log((1-x)/(x+saturation_eps))

def g_invert_continuous_bernoulli(x, params=None):
    y = deepcopy(x)
    y[y==0] = - np.inf
    y[y==1] = np.inf
    return y

def g_invert_gaussian(x, params=None):
    return x

def g_invert_poisson(x, params=None):
    y = deepcopy(x)
    y[y==0] = - np.inf
    y[y>0] = torch.log(y[y>0])
    return y

def g_invert_negative_binomial(x, params=None):
    r = params['r']
    return torch.log(x/(x+r))

def g_invert_negative_binomial_reparametrized(x, params=None):
    r = params['r']
    # return - torch.log((2*x+r)/ r / (x+r))
    # return torch.log(x)
    if r.shape[0] != x.shape[1]:
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        r = r[params['gene_filter']].to(device)
        return torch.log(r * r / x)
    else:
        return torch.log(r * r / x)

def g_invert_beta(x, params=None):
    beta = params['beta']
    n_jobs = params['n_jobs'] if 'n_jobs' in params else 1

    return torch.Tensor(Parallel(n_jobs=n_jobs, verbose=10)(
        delayed(compute_alpha_gene)(beta[j], x[:,j], eps=10**(-8))
        for j in range(x.shape[1])
    )).T

def g_invert_beta_reparametrized(x, params=None):
    nu = params['nu']
    n_jobs = params['n_jobs'] if 'n_jobs' in params else 1

    return torch.Tensor(Parallel(n_jobs=n_jobs, verbose=10)(
        delayed(compute_mu_gene)(nu[j], x[:,j], eps=10**(-6), maxiter=1000)
        for j in range(x.shape[1])
    )).T

def g_invert_log_normal(x, params=None):
    nu = params['nu']
    n_jobs = params['n_jobs'] if 'n_jobs' in params else 1

    return torch.log(x.clip(LOG_NORMAL_ZERO_THRESHOLD))

def g_invert_gamma(x, params=None):
    nu = params['nu']
    n_jobs = params['n_jobs'] if 'n_jobs' in params else 1

    # TO COMPUTE
    
    return torch.Tensor(Parallel(n_jobs=n_jobs, verbose=10)(
        delayed(compute_gamma_saturated_params_gene)(nu[j], x[:,j].clip(GAMMA_ZERO_THRESHOLD), eps=10**(-6), maxiter=100)
        for j in range(x.shape[1])
    )).T

def g_invertfun(family):
    if family == 'bernoulli':
        return g_invert_bernoulli
    if family == 'continuous_bernoulli':
        return g_invert_continuous_bernoulli
    elif family == 'poisson':
        return g_invert_poisson
    elif family == 'gaussian':
        return g_invert_gaussian
    elif family == 'multinomial':
        raise NotImplementedError('multinomial not implemented')
    elif family.lower() in ['negative_binomial', 'nb']:
        return g_invert_negative_binomial
    elif family.lower() in ['negative_binomial_reparam', 'nb_rep']:
        return g_invert_negative_binomial_reparametrized
    elif family.lower() in ['beta']:
        return g_invert_beta
    elif family.lower() in ['beta_reparam', 'beta_rep']:
        return g_invert_beta_reparametrized
    elif family.lower() in ['log_normal', 'log normal', 'lognorm']:
        return g_invert_log_normal
    elif family.lower() in ['gamma']:
        return g_invert_gamma


# Functions h
def h_gaussian(x, params=None):
    return np.power(2*np.pi, -1)

def h_bernoulli(x, params=None):
    return 1.

def h_continuous_bernoulli(x, params=None):
    return 1.

def h_poisson(x, params=None):
    return torch.Tensor(scipy.special.gamma(x+1))

def h_multinomial(x, params=None):
    pass

def h_negative_binomial(x, params=None):
    r = params['r']
    return torch.Tensor(scipy.special.binom(x+r-1, x))

def h_beta(x, params=None):
    return 1 / (x * (1-x))

def h_log_norm(x, params=None):
    return 1 / (torch.sqrt(2*pi_val) * x)

def h_gamma(x, params=None):
    return 1

def h_fun(family):
    if family == 'bernoulli':
        return h_bernoulli
    elif family == 'continuous_bernoulli':
        return h_continuous_bernoulli
    elif family == 'poisson':
        return h_poisson
    elif family == 'gaussian':
        return h_gaussian
    elif family == 'multinomial':
        raise NotImplementedError('multinomial not implemented')
    elif family.lower() in ['negative_binomial', 'nb', 'negative_binomial_reparam', 'nb_rep']:
        return h_negative_binomial
    elif family.lower() in ['log_normal', 'log normal', 'lognorm']:
        return h_log_norm
    elif family.lower() in ['gamma']:
        return h_gamma


def log_h_negative_binomial(x, params=None):
    r = params['r']

    return torch.lgamma(x+r) - torch.lgamma(x+1) - torch.lgamma(r)

def log_h_gamma(x, params=None):
    return 0

def log_h_fun(family):
    if family == 'bernoulli':
        return NotImplementedError('bernoulli not implemented')
    elif family == 'continuous_bernoulli':
        return NotImplementedError('cont bernoulli not implemented')
    elif family == 'poisson':
        return NotImplementedError('poisson not implemented')
    elif family == 'gaussian':
        return NotImplementedError('multinomial not implemented')
    elif family == 'multinomial':
        raise NotImplementedError('multinomial not implemented')
    elif family.lower() in ['negative_binomial', 'nb', 'negative_binomial_reparam', 'nb_rep']:
        return log_h_negative_binomial
    elif family.lower() in ['gamma']:
        return log_h_gamma


# Compute likelihood
def likelihood(family, data, theta, params=None):
    h = h_fun(family)(data, params)
    exp_term = expt_term(family)(data, theta, params)
    partition =  G_fun(family)(theta, params)
    return h * torch.exp(exp_term - partition)


def log_likelihood(family, data, theta, params=None):
    h = log_h_fun(family)(data, params)
    exp_term = expt_term(family)(data, theta, params)
    partition = G_fun(family)(theta, params)
    return - h - exp_term + partition

def natural_parameter_log_likelihood(family, data, theta, params=None):
    exp_term = expt_term(family)(data, theta, params)
    partition = G_fun(family)(theta, params)
    return - exp_term + partition

def make_saturated_loading_cost(family, max_value=np.inf, params=None, train=True):
    """
    Constructs the likelihood function for a given family.
    """
    loss = G_fun(family)
    exp_term_fun = expt_term(family)
    inner_params = params
    
    def likelihood(X, data, parameters, intercept=None):
        intercept = intercept if intercept is not None else torch.zeros(parameters.shape[1])

        # Project saturated parameters
        eta = torch.matmul(parameters - intercept, torch.matmul(X, X.T)) + intercept
        if not train:
            eta = eta.clip(-max_value, max_value)

        # Compute the log-partition on projected parameters
        c = loss(eta, params)
        c = torch.sum(c)

        # Second term (with potential parametrization)
        d = exp_term_fun(data, eta, inner_params)
        d = torch.sum(d)
        return c - d
    
    return likelihood