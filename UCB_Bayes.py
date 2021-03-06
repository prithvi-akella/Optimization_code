import numpy as np
import math
import matplotlib.pyplot as plt
from scipy.stats import norm
from scipy.optimize import minimize
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import ConstantKernel, Matern, RBF
from multiprocessing import Pool
import matplotlib
import matplotlib.pyplot as plt
from matplotlib import colors as mcolors
from matplotlib import rc, font_manager, cm
import pdb
import pickle
import time

matplotlib.rcParams['text.latex.preamble'] = [r'\usepackage{amsmath,amssymb}']
rc('font',**{'family':'sans-serif','sans-serif':['Helvetica']})
rc('text', usetex=True)
colors = dict(mcolors.BASE_COLORS, **mcolors.CSS4_COLORS)
plt.rcParams.update({'font.size':24})
plt.rcParams["figure.figsize"] = (12.8,8)

class UCBOptimizer:
	def __init__(self, objective, bounds, B, R=1, delta=0.05, n_restarts = 0,
		n_init_samples = 5, tolerance = 0.05, length_scale = 1, constraints = None,
		avail_processors = 1, debug = False, verbose = True):
		# Objective here encodes the objective function to be optimized
		# Bounds indicates the bounds over which objective is to be optimized.
		# This algorithm will assume that the bounding region is hyper-rectangular
		# as this assumption can be made trivially (if Objective is to be optimized
		# over a compact, non-rectangular space, and if Objective is continuous over
		# said space, then define a continuous, surjective map from a larger hyper-
		# rectangular space to the compact space in question.  The resulting composite
		# function is a continuous objective function which still optimizes the original).

		# Objective should meet the criteria that objective(x) = y, where x is an 1 x N
		# numpy array and y is a float.

		# Bounds should be an Nx2 numpy array, where N is the dimensionality of the
		# constraint space.  The first column contains the lower bounds, the second the
		# upper bound.

		# B is the presumed upper bound on the RHKS norm for the Objective in question

		# R is the presumed upper bound on the variance of the measurement noise (should
		# there not be measurement noise, R will default to 1)

		# 1-delta is the required confidence, i.e. at termination the result will hold
		# with probability 1-delta (should the bounding values be correct)

		# n_restarts is the number of times the GPR hyperparameters will be re-initialized when
		# fitting (larger n_restarts indicates longer time to convergence, default value = 0 implying
		# that the hyperparameters will not be optimized.  In general, the hyperparameters needn't be
		# optimized as the algorithm will use the universal Matern Kernel or the squared exponential kernel)

		# n_init_samples is the number of samples the algorithm will start off with, the default will be 5,
		# and they will be randomly sampled from the feasible, hyper-rectangular space.  For problems
		# of a high dimension, seeding with a larger number of initial samples will likely expedite the
		# solution process, though it will lead to large runtimes for GPR regression in later stages (as
		# the number of samples explodes).

		# tolerance prescribes the required tolerance within which we would like the optimal value
		# to lie

		# length_scale: if the length_scale of the objective function is known apriori, then please input it
		# (for use in an RBF kernel), else it will be initialized to 1 (for which the kernel is universal)

		# constraint will be initialized to None unless otherwise populated.  We presume constraints is 
		# a list of constraint functions for the optimization problem at hand, each of which has the same
		# evaluation scheme, i.e.: constraints[i](x) = some float.  Here, i is the i-th constraint, and
		# x is a 1xN numpy array.  Additionally, we assume wlog that each constraint function is to be kept
		# negative, i.e. the constraints are constraint[i](x) <= 0.

		self.objective = objective
		self.bounds = bounds
		self.beta = []
		self.B = B
		self.R = R
		self.delta = delta
		self.mu = []                      # Initializing fields to contain the mean function and
		self.sigma = []                   # covariance function respectively.
		self.dimension = bounds.shape[0]  # Dimensionality of the feasible space
		self.X_sample = None              # Instantiating a variable to keep track of all sampled, X values
		self.Y_sample = None              # Instantiating a variable to keep track of all sampled, Y values
		self.cmax =  -1e6                 # Instantiating a variable to keep track of the current best value
		self.best_sample = None           # Instantiating a variable to keep track of the current best sample
		self.n_init_samples = n_init_samples
		self.n_restarts = n_restarts
		self.tol = tolerance
		self.max_val = []
		self.term_sigma = 0
		self.UCB_sample_pt = 0
		self.UCB_val = -1e6
		self.constraints = constraints
		self.length_scale = length_scale
		self.avail_processors = avail_processors
		self.debug = debug
		self.xbase = np.linspace(self.bounds[0,0], self.bounds[0,1], 500).tolist()
		self.verbose = verbose
		self.kernel = RBF(self.length_scale)

	def check_constraints(self,x):
		if self.constraints == None:
			return True
		else:
			flag = True
			for constraint in self.constraints:
				flag = flag and (constraint(x) <=0)
				if flag == False: break
			return flag

	def initialize(self, sample = None, observations = None, init_flag = False):
		# Initialize a starting set of samples and their y-values.  Initial sample size is set to 5

		if init_flag == False:
			sample_flag = False
			while not sample_flag:
				self.X_sample = np.random.uniform(self.bounds[:,0], self.bounds[:,1],
					size=(self.n_init_samples,self.dimension))
				met_constraints_flags = []
				for i in range(self.n_init_samples):
					met_constraints_flags.append(self.check_constraints(self.X_sample[i,:].reshape(1,-1)))
				sample_flag = sample_flag or all(met_constraints_flags)

			self.Y_sample = np.zeros((self.n_init_samples,1))
			for i in range(self.n_init_samples):
				self.Y_sample[i,0] = self.objective(self.X_sample[i,:].reshape(1,-1))
				if self.Y_sample[i] > self.cmax:
					self.cmax = self.Y_sample[i,0]
					self.best_sample = self.X_sample[i,:].reshape(1,-1)
		elif sample is not None:
			self.X_sample = sample
			self.Y_sample = observations
			self.cmax = np.max(self.Y_sample)
			observation_list = observations.reshape(-1,).tolist()
			max_index = observation_list.index(max(observation_list))
			self.best_sample = self.X_sample[max_index,:].reshape(1,-1)
		else:
			error('The initialization flag was set to False and a prior sample set was not provided')

	def UCB(self, x):
		# Calculate the Upper Confidence Bound for a value, x, based on the data-set, (x_sample, y_sample).
		# gpr is the Regressed Gaussian Process defining the current mean and standard deviation.

		# Returning the UCB based on the choice of beta.
		if self.check_constraints(x.reshape(1,-1)):
			return self.mu(x) + self.beta*self.sigma(x)
		else:
			return -100

	def propose_location(self, opt_restarts = 20, granularity = 50):
		# Propose the next location to sample (identify the sample point that maximizes the UCB)
		# This is a nonlinear optimization problem and will require a number of restarts
		# The number of restarts will be set to min(N_samples+1,25) to expedite computation

		min_value = 1e6      # Keeping track of the current minimum value (min of negation of UCB)
		min_x = None         # Keeping track of best sample point so far

		def min_obj(x):
			return -self.UCB(x=x)

		if self.bounds.shape[0] > 1:


			# Iterate through opt_restarts IP methods to determine the maximizer of the UCB over the
			# hyper-rectangle identified by bounds.
			if self.constraints is not None:
				x0_array = np.array([])
				init_state_count = 0
				while init_state_count <= opt_restarts:
					sample = np.random.uniform(self.bounds[:,0], self.bounds[:,1], size = (1,self.dimension))
					if self.check_constraints(sample):
						x0_array = np.vstack((x0_array,sample)) if x0_array.shape[0]>0 else sample
						init_state_count += 1
			else:
				x0_array = np.random.uniform(self.bounds[:, 0], self.bounds[:, 1], size=(opt_restarts, self.dimension))

			for x0 in x0_array:
				res = minimize(min_obj, x0 = x0, bounds = self.bounds, method='L-BFGS-B')
				if res.fun < min_value:
					min_value = res.fun
					min_x = res.x

			# Output the minimizer (which is the maximizer of the UCB as we're minimizing -UCB)
			return min_x.reshape(1,-1), min_value
		else:
			ub = [self.mu(np.array([[x]])) + self.beta*self.sigma(np.array([[x]])) for x in self.xbase]
			maxval = max(ub)
			maxloc = self.xbase[ub.index(maxval)]
			res = minimize(min_obj, x0 = np.array([[maxloc]]), bounds = self.bounds, method = 'L-BFGS-B')

			return res.x.reshape(1,-1), res.fun

	def calc_musigma(self):
		self.Kn = self.kernel(self.X_sample)
		t = self.X_sample.shape[0]
		eta = 2/t
		self.KI = self.Kn + (1+eta)*np.identity(self.Kn.shape[0])
		self.Kinv = np.linalg.inv(self.KI)
		self.knx = lambda x: self.kernel(self.X_sample,x.reshape(1,-1))
		# self.mu = lambda x: np.dot(np.dot(self.kernel(x.reshape(1,-1), self.X_sample).reshape(1,-1),
		# 	Kinv),self.Y_sample)[0,0]
		# self.sigma = lambda x: (self.kernel(x.reshape(1,-1)) - np.dot(np.dot(self.kernel(x.reshape(1,-1), self.X_sample).reshape(1,-1), Kinv),self.kernel(x.reshape(1,-1), self.X_sample).reshape(-1,1)))[0,0]
		self.mu = lambda x: (self.knx(x).transpose() @ self.Kinv @ self.Y_sample)[0,0]
		self.sigma = lambda x: (1 - self.knx(x).transpose() @ self.Kinv @ self.knx(x))[0,0]

		innersqrt = np.linalg.det(self.KI)
		self.beta = self.B + self.R*math.sqrt(2*math.log(math.sqrt(innersqrt)/self.delta))
		pass

	def optimize(self):
		# Initialize the Squared Exponential Kernel (known to be universal for any parameters)
		F = 100
		t = 0
		missed_constraints = 0
		indeces = []

		self.calc_musigma()
		new_x, min_val = self.propose_location(opt_restarts = self.X_sample.shape[0]*2)
		F = 2*self.beta*self.sigma(new_x)
		self.UCB_sample_pt = new_x
		self.UCB_val = -min_val

		while F >= self.tol:

			start = time.time()
			self.term_sigma = self.sigma(new_x)
			new_y = self.objective(new_x)
			print('Evaluating new objective function and calculating terminating sigma: %.4f'%(time.time() - start))

			if not self.check_constraints(new_x):
				missed_constraints += 1
				indeces.append(self.X_sample.shape[0])

			if new_y > self.cmax:
				self.cmax = new_y
				self.best_sample = new_x

			self.X_sample = np.vstack((self.X_sample, new_x))
			self.Y_sample = np.vstack((self.Y_sample, new_y))

			print('Finshed with iteration %d'%t)
			print('UCB value at that point: %.5f'%self.UCB_val)
			print('Current F value: %.5f'%F)
			print('Required tolerance: %.5f'%self.tol)
			print('')

			start = time.time()
			self.calc_musigma()
			print('Calculating new mu/sigma: %.4f'%(time.time() - start))
			start = time.time()
			new_x, min_val = self.propose_location(opt_restarts = self.X_sample.shape[0]*2)
			print('Finding new location: %.4f'%(time.time() - start))
			F = 2*self.beta*self.sigma(new_x)
			self.UCB_sample_pt = new_x
			self.UCB_val = -min_val
			t+=1

			if self.debug and (t%25==0):
				self.plot_approximation()

			if t%250 == 0 and self.bounds.shape[0] == 1:
				self.xbase = np.linspace(self.bounds[0,0], self.bounds[0,1], (t // 250 + 2)*250).tolist()

			if F < self.tol:
				print('Possibly found an answer, verifying if answer is correct')
				new_x, min_val = self.propose_location(opt_restarts = self.X_sample.shape[0]*10)
				F = 2*self.beta*self.sigma(new_x)
				self.UCB_sample_pt = new_x
				self.UCB_val = -min_val



		print('Calculated maximum value: %.5f'%(-min_val))
		print('beta at termination: %.5f'%self.beta)
		print('Final variance at termination: %.5f'%self.term_sigma)
		print('Final F at termination: %.5f'%F)
		print('')
		self.final_iteration = t
		if self.constraints is not None: print('Total number of times samples taken that did not satisfy constraints: %d'%missed_constraints)
		if self.constraints is not None: print('Indeces in X_sample where the offending samples were taken {}'.format(indeces))

		if self.bounds.shape[0] == 1:
			self.xbase = np.linspace(self.bounds[0,0], self.bounds[0,1], 100).tolist()
			self.mean_values = [None for i in range(100)]
			self.ub = [None for i in range(100)]
			self.lb = [None for i in range(100)]
			for i in range(100):
				xval = np.array([[self.xbase[i]]])
				self.mean_values[i] = self.mu(np.array(xval))
				self.ub = self.mean_values[i] + self.beta*self.sigma(xval)
				self.lb = self.mean_values[i] - self.beta*self.sigma(xval)
		elif self.bounds.shape[0] == 2:
			x = np.linspace(self.bounds[0,0], self.bounds[0,1], 50)
			y = np.linspace(self.bounds[1,0], self.bounds[1,1], 50)
			X,Y = np.meshgrid(x,y)
			self.Xbase = X
			self.Ybase = Y
			self.mean_values = np.zeros((50,50))
			self.ub = np.zeros((50,50))
			self.lb = np.zeros((50,50))
			for i in range(50):
				for j in range(50):
					xval = np.array([[X[i,j], Y[i,j]]])
					self.mean_values[i,j] = self.mu(xval)
					self.ub[i,j] = self.mu(xval) + self.beta*self.sigma(xval)
					self.lb[i,j] = self.mu(xval) - self.beta*self.sigma(xval)

	def plot_approximation(self, smoothness = 500, figtitle = None, save = False, fname = None, label = None):
		'''
		Useful for debugging purposes only.  Will plot the 1-d objective function over the feasible space and plot the GPR approximation
		'''
		if self.bounds.shape[0] == 1:
			xbase = np.linspace(self.bounds[0,0], self.bounds[0,1], 500).tolist()
			true_val = [self.debug(x) for x in xbase]
			ub = [self.mu(np.array([[x]])) + self.beta*self.sigma(np.array([[x]])) for x in xbase]
			lb = [self.mu(np.array([[x]])) - self.beta*self.sigma(np.array([[x]])) for x in xbase]
			maxval = max(ub)
			maxloc = xbase[ub.index(maxval)]
			fig, ax = plt.subplots()
			if label is not None:
				ax.plot(xbase, true_val, lw = 5, color = colors['black'], label = label)
			else:
				ax.plot(xbase, true_val, lw = 5, color = colors['black'])
			ax.fill_between(xbase, lb, ub, alpha = 0.5, color = colors['gray'], label = r'$\mathrm{GP~bounds}$')
			ax.hlines(maxval, xmin = xbase[0], xmax = xbase[-1], lw = 5, color = colors['red'], ls = '--')
			# ax.vlines(maxloc, ymin = ax.get_ylim()[0], ymax = ax.get_ylim()[1], lw = 5, color = colors['red'], ls = '--')
			ax.set_xlabel(r'$x$')
			ax.set_ylabel(r'$y$', rotation = 0)
			sample_flag = input('Show samples this time? (y/n): ')
			if sample_flag == 'y':
				sample_flag = None
				ax.scatter(self.X_sample, self.Y_sample, marker = 'x', s = 50, color = colors['green'], label = r'$\mathrm{samples}$')
			if figtitle is not None: ax.set_title(figtitle)
			ax.grid(lw = 3, alpha = 0.5)
			ax.legend(loc = 'best')
			plt.tight_layout()
			if save: plt.savefig(fname = fname, bbox_inches = 'tight', pad_inches = 0.1, dpi = 200)
			plt.show()
			command = input('Enter debugger mode? (y/n): ')
			if command == 'y':
				command = None
				pdb.set_trace()
			pass
		elif self.bounds.shape[0] == 2:
			xbase = np.linspace(self.bounds[0,0],self.bounds[0,1],100)
			ybase = np.linspace(self.bounds[1,0],self.bounds[1,1],100)
			X,Y = np.meshgrid(xbase,ybase)
			Z = np.zeros((100,100))
			US = np.zeros((100,100))
			LS = np.zeros((100,100))
			for i in range(100):
				for j in range(100):
					x = np.array([[X[i,j],Y[i,j]]])
					Z[i,j] = self.debug(x)
					US[i,j] = self.mu(x) + self.beta*self.sigma(x)
					LS[i,j] = self.mu(x) - self.beta*self.sigma(x)
			fig, ax = plt.subplots(subplot_kw = dict(projection = '3d'))
			ax.plot_surface(X,Y,Z, color = colors['black'])
			ax.plot_surface(X,Y,US, color = colors['red'], alpha = 0.5)
			ax.plot_surface(X,Y,LS, color = colors['red'], alpha = 0.5)
			plt.show()
			print('Entering debugging mode:')
			pdb.set_trace()
			pass

	

