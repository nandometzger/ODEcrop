###########################
# Latent ODEs for Irregularly-Sampled Time Series
# Author: Yulia Rubanova,
# Editor: Nando Metzger
###########################

import os
import sys
import traceback
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot
import matplotlib.pyplot as plt

import time
import datetime
import argparse
import numpy as np
import pandas as pd
from random import SystemRandom
from sklearn import model_selection

import torch
import torch.nn as nn
from torch.nn.functional import relu
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter

import lib.utils as utils
from lib.plotting import *

from lib.rnn_baselines import *
from lib.ode_rnn import *
from lib.create_latent_ode_model import create_LatentODE_model
from lib.parse_datasets import parse_datasets
from lib.ode_func import ODEFunc, ODEFunc_w_Poisson
from lib.diffeq_solver import DiffeqSolver
from mujoco_physics import HopperPhysics

from lib.utils import compute_loss_all_batches

# Nando's additional libraries
from tqdm import tqdm
from hyperopt import hp
from hyperopt import fmin, tpe, space_eval, Trials

from lib.training import construct_and_train_model, train_it
from lib.utils import hyperopt_summary

# Generative model for noisy data based on ODE
parser = argparse.ArgumentParser('Latent ODE')
parser.add_argument('-n',  type=int, default=30000, help="Size of the dataset")
parser.add_argument('-validn',  type=int, default=1000, help="Size of the validation dataset")
parser.add_argument('--niters', type=int, default=1) # default=300
parser.add_argument('--lr',  type=float, default=1e-2, help="Starting learning rate.")
parser.add_argument('-b', '--batch-size', type=int, default=20000)
parser.add_argument('--viz', default=True, action='store_true', help="Show plots while training")

parser.add_argument('--save', type=str, default='experiments/', help="Path for save checkpoints")
parser.add_argument('--load', type=str, default=None, help="ID of the experiment to load for evaluation. If None, run a new experiment.")
parser.add_argument('-r', '--random-seed', type=int, default=1991, help="Random_seed")

parser.add_argument('--dataset', type=str, default='crop', help="Dataset to load. Available: physionet, activity, hopper, periodic")
parser.add_argument('-s', '--sample-tp', type=float, default=None, help="Number of time points to sub-sample."
	"If > 1, subsample exact number of points. If the number is in [0,1], take a percentage of available points per time series. If None, do not subsample")

parser.add_argument('-c', '--cut-tp', type=int, default=None, help="Cut out the section of the timeline of the specified length (in number of points)."
	"Used for periodic function demo.")

parser.add_argument('--quantization', type=float, default=0.1, help="Quantization on the physionet dataset."
	"Value 1 means quantization by 1 hour, value 0.1 means quantization by 0.1 hour = 6 min")

parser.add_argument('--latent-ode', action='store_true', help="Run Latent ODE seq2seq model")
parser.add_argument('--z0-encoder', type=str, default='odernn', help="Type of encoder for Latent ODE model: odernn or rnn")

parser.add_argument('--classic-rnn', action='store_true', help="Run RNN baseline: classic RNN that sees true points at every point. Used for interpolation only.")
parser.add_argument('--rnn-cell', default="gru", help="RNN Cell type. Available: gru (default), expdecay")
parser.add_argument('--input-decay', action='store_true', help="For RNN: use the input that is the weighted average of impirical mean and previous value (like in GRU-D)")
parser.add_argument('--ode-rnn', default=True, action='store_true', help="Run ODE-RNN baseline: RNN-style that sees true points at every point. Used for interpolation only.")
parser.add_argument('--rnn-vae', default=False, action='store_true', help="Run RNN baseline: seq2seq model with sampling of the h0 and ELBO loss.")

parser.add_argument('-l', '--latents', type=int, default=15, help="Size of the latent state")
parser.add_argument('--rec-dims', type=int, default=100, help="Dimensionality of the recognition model (ODE or RNN).")

parser.add_argument('--rec-layers', type=int, default=4, help="Number of layers in ODE func in recognition ODE") 
parser.add_argument('--gen-layers', type=int, default=2, help="Number of layers in ODE func in generative ODE")

parser.add_argument('-u', '--units', type=int, default=500, help="Number of units per layer in ODE func")
parser.add_argument('-g', '--gru-units', type=int, default=50, help="Number of units per layer in each of GRU update networks")

parser.add_argument('--poisson', action='store_true', help="Model poisson-process likelihood for the density of events in addition to reconstruction.")
parser.add_argument('--classif', default="True", action='store_true', help="Include binary classification loss -- used for Physionet dataset for hospiral mortality")

parser.add_argument('--linear-classif', default=False, action='store_true', help="If using a classifier, use a linear classifier instead of 1-layer NN")
parser.add_argument('--extrap', action='store_true', help="Set extrapolation mode. If this flag is not set, run interpolation mode.")

parser.add_argument('-t', '--timepoints', type=int, default=100, help="Total number of time-points")
parser.add_argument('--max-t',  type=float, default=5., help="We subsample points in the interval [0, args.max_tp]")
parser.add_argument('--noise-weight', type=float, default=0.01, help="Noise amplitude for generated traejctories")
parser.add_argument('--tensorboard',  action='store_true', default=True, help="monitor training with the help of tensorboard")
parser.add_argument('--ode-method', type=str, default='euler',
					help="Method of the ODE-Integrator. One of: 'explicit_adams', fixed_adams', 'adams', 'tsit5', 'dopri5', 'bosh3', 'euler', 'midpoint', 'rk4' , 'adaptive_heun' ")
parser.add_argument('--optimizer', type=str, default='adamax',
					help="Chose from: adamax (default), adagrad, adadelta, adam, adaw, sparseadam, ASGD, RMSprop, rprop, SGD")
					# working: adamax, adagrad, adadelta, adam, adaw, ASGD, rprop
					# not working sparseadam(need sparse gradients), LBFGS(missing closure), RMSprop(CE loss is NAN)

args = parser.parse_args()

#print("I'm running on GPU") if torch.cuda.is_available() else print("I'm running on CPU")
if torch.cuda.device_count() > 1:
	num_gpus_avail = torch.cuda.device_count()
	print("I'm counting gpu's: ", num_gpus_avail)
	print("Means I will train ", , " models, with different random seeds")

if num_gpus_avail>1:
	for ind in num_gpus_avail:
		device.append("cuda:" + ind)
else:
	device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


file_name = os.path.basename(__file__)[:-3]
utils.makedirs(args.save)

#####################################################################################################

if __name__ == '__main__':

	experimentID = args.load

	print("Sampling dataset of {} training examples".format(args.n))
	
	input_command = sys.argv
	ind = [i for i in range(len(input_command)) if input_command[i] == "--load"]
	if len(ind) == 1:
		ind = ind[0]
		input_command = input_command[:ind] + input_command[(ind+2):]
	input_command = " ".join(input_command)

	utils.makedirs("results/")

	##################################################################
	# Dataset

	data_obj = parse_datasets(args, device)
	
	##################################################################

	#Load checkpoint and evaluate the model
	if args.load is not None:
		#utils.get_ckpt_model(ckpt_path, model, device)
		utils.get_ckpt_model(top_ckpt_path, model, device)
		exit()

	#################################################################
	# Hyperparameter Optimization
	

	# create a specification dictionary for training
	spec_config = {
			"args": args,
			"data_obj": data_obj,
			"args": args,
			"file_name": file_name,
			"experimentID": experimentID,
			"input_command": input_command,
			"device": device
		},
	
	hyper_config = {
		"spec_config": spec_config, # fixed argument space

		#"rec_layers": hp.quniform('rec_layers', 1, 4, 1),
		#"units": hp.quniform('ode_units', 20, 500, 10), # default: 500
		#"latents": hp.quniform('latents', 5, 65, 5), # default: 35
		#"gru-units": hp.quniform('gru-units', 5, 70, 5), # default: 50
		"optimizer": hp.choice('optimizer',['adamax', 'adam', 'SGD']), #['adamax', 'adagrad', 'adadelta', 'adam', 'adaw', 'ASGD', 'rprop', 'SGD']
		"lr": hp.loguniform('lr', 0.001, 0.1),
		#"random-seed":  hp.randint('seed', 5)
	}

	try:
		trials = Trials()
		best = fmin(construct_and_train_model,
			hyper_config,
			trials=trials,
			algo=tpe.suggest,
			max_evals=30)

	except KeyboardInterrupt:
		best=None
		hyperopt_summary(trials)

	except Exception:
		hyperopt_summary(trials)
		traceback.print_exc(file=sys.stdout)

	hyperopt_summary(trials)


	"""
	SPACE = {
		"rec_layers": hp.quniform('rec_layers', 1, 5, 1),
		"units": hp.quniform('ode_units', 1, 500, 1), # default: 500
		#"latents": hp.quniform('latents', 5, 65, 1), # default: 35
		#"gru-units": hp.quniform('latents', 5, 70, 1), # default: 50
		#"optimizer": hp.choice('optimizer',['adamax', 'adagrad', 'adadelta', 'adam', 'adaw', 'sparseadam', 'ASGD', 'LBFGS', 'RMSprop', 'rprop', 'SGD']),
		#"lr": hp.loguniform('lr', 0.0001, 0.1),
		#"random-seed":  hp.randint('seed', 5)
		'learning_rate': 
			hp.loguniform('learning_rate',np.log(0.01),np.log(0.5)),
		'max_depth': 
			hp.choice('max_depth', range(1, 30, 1)),
		'num_leaves': 
			hp.choice('num_leaves', range(2, 100, 1)),
		'subsample': 
			hp.uniform('subsample', 0.1, 1.0)
	}
	"""



