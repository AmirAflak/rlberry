from copy import deepcopy
from datetime import datetime
from joblib import Parallel, delayed
import logging
import os
import pickle

import rlberry.seeding as seeding
from rlberry.stats.evaluation import compare_policies


_OPTUNA_INSTALLED = True
try:
    import optuna
except Exception:
    _OPTUNA_INSTALLED = False


#
# Main class
#

class AgentStats:
    """
    Class to train, optimize hyperparameters, evaluate and gather
    statistics about an agent.
    """
    def __init__(self,
                 agent_class,
                 train_env,
                 eval_env=None,
                 eval_horizon=None,
                 init_kwargs=None,
                 fit_kwargs=None,
                 policy_kwargs=None,
                 agent_name=None,
                 n_fit=4,
                 n_jobs=4,
                 output_dir='stats_data',
                 verbose=5):
        """
        Parameters
        ----------
        agent_class
            Class of the agent.
        train_env : Model
            Enviroment used to initialize/train the agent.
        eval_env : Model
            Environment used to evaluate the agent. If None, set to a
            reseeded deep copy of train_env.
        init_kwargs : dict
            Arguments required by the agent's constructor.
        fit_kwargs : dict
            Arguments required to train the agent.
        policy_kwargs : dict
            Arguments required to call agent.policy().
        agent_name : str
            Name of the agent. If None, set to agent_class.name
        n_fit : int
            Number of agent instances to fit.
        n_jobs : int
            Number of jobs to train the agents in parallel using joblib.
        output_dir : str
            Directory where to store data by default.
        verbose : int
            Verbosity level.
        """
        # agent_class should only be None when the constructor is called
        # by the class method AgentStats.load(), since the agent class
        # will be loaded.
        if agent_class is not None:

            self.agent_name = agent_name
            if agent_name is None:
                self.agent_name = agent_class.name

            # create oject identifier
            timestamp = datetime.timestamp(datetime.now())
            self.identifier = 'stats_{}_{}'.format(self.agent_name,
                                                   str(int(timestamp)))

            self.fit_info = agent_class.fit_info
            self.agent_class = agent_class
            self.train_env = train_env
            if eval_env is None:
                self.eval_env = deepcopy(train_env)
                self.eval_env.reseed()
            else:
                self.eval_env = deepcopy(eval_env)
                self.eval_env.reseed()

            self.eval_horizon = eval_horizon
            # init and fit kwargs are deep copied in fit()
            self.init_kwargs = init_kwargs
            self.fit_kwargs = fit_kwargs
            self.policy_kwargs = deepcopy(policy_kwargs)
            self.n_fit = n_fit
            self.n_jobs = n_jobs
            self.output_dir = output_dir
            self.verbose = verbose

            if init_kwargs is None:
                self.init_kwargs = {}
            if fit_kwargs is None:
                self.fit_kwargs = {}
            if policy_kwargs is None:
                self.policy_kwargs = {}

            # Create environment copies for training
            self.train_env_set = []
            for _ in range(n_fit):
                _env = deepcopy(train_env)
                _env.reseed()
                self.train_env_set.append(_env)

            #
            self.fitted_agents = None
            self.fit_statistics = {}

            #
            self.rng = seeding.get_rng()

            # optuna study
            self.study = None

            # default filename to save data
            self.default_filename = os.path.join(self.output_dir,
                                                 self.identifier)

    def fit(self):
        if self.verbose > 0:
            print("\n Training AgentStats for %s... \n" % self.agent_name)
        args = [(self.agent_class, train_env,
                deepcopy(self.init_kwargs), deepcopy(self.fit_kwargs))
                for train_env in self.train_env_set]

        workers_output = Parallel(n_jobs=self.n_jobs, verbose=self.verbose)(
            delayed(_fit_worker)(arg) for arg in args)

        self.fitted_agents, stats = (
            [i for i, j in workers_output],
            [j for i, j in workers_output])

        if self.verbose > 0:
            print("\n ... trained! \n")

        # gather all stats in a dictionary
        for entry in self.fit_info:
            self.fit_statistics[entry] = []
            for stat in stats:
                self.fit_statistics[entry].append(stat[entry])

    def save(self, filename=None, **kwargs):
        """
        Parameters
        ----------
        filename : string
            Filename with .pickle extension.
            If None, default_filename attribute is used.
        """
        if filename is None:
            filename = self.default_filename
            if not os.path.exists(self.output_dir):
                os.makedirs(self.output_dir)

        if filename[-7:] != '.pickle':
            filename += '.pickle'

        with open(filename, 'wb') as ff:
            pickle.dump(self.__dict__, ff)

    @classmethod
    def load(cls, filename):
        if filename[-7:] != '.pickle':
            filename += '.pickle'

        obj = cls(None, None)
        with open(filename, 'rb') as ff:
            tmp_dict = pickle.load(ff)
        obj.__dict__.clear()
        obj.__dict__.update(tmp_dict)
        return obj

    def optimize_hyperparams(self, n_trials=5, timeout=60, n_sim=5, n_fit=2,
                             n_jobs=2,  sampler_method='random',
                             pruner_method='none', continue_previous=False):
        """
        Run hyperparameter optimization and updates init_kwargs with the
        best hyperparameters found.

            Note: pruning not yet implemented.


            Currently supported sampler_method:
                'random'
                'optuna_default'


        Parameters
        ----------
        n_trials: int
            Mumber of agent evaluations
        timeout: int
            Stop study after the given number of second(s).
            Set to None for unlimited time.
        n_sim : int
            Number of Monte Carlo simulations to evaluate a policy.
        n_fit: int
            Number of agents to fit for each hyperparam evaluation.
        n_jobs: int
            Number of jobs to fit agents for each hyperparam evaluation,
            and also the number of jobs of optuna.
        sampler_method : str
            Optuna sampling method.
        pruner_method : str
            Optuna pruner method.
        continue_previous : bool
            Set to true to continue previous optuna study. If true,
            sampler_method and pruner_method will be
            the same as in the previous study.
        """
        global _OPTUNA_INSTALLED
        if not _OPTUNA_INSTALLED:
            logging.error("Optuna not installed.")
            return

        assert self.eval_horizon is not None, \
            "To use optimize_hyperparams(), \
eval_horizon must be given to AgentStats."

        #
        # Create optuna study
        #
        if continue_previous:
            assert self.study is not None
            study = self.study

        else:
            # get sampler
            if sampler_method == 'random':
                optuna_seed = self.rng.integers(2**16)
                sampler = optuna.samplers.RandomSampler(seed=optuna_seed)
            elif sampler_method == 'optuna_default':
                sampler = None
            else:
                raise NotImplementedError("Sampler method %s is\
 not implemented." % sampler_method)

            # get pruner
            if pruner_method == 'halving':
                pruner = optuna.pruners.SuccessiveHalvingPruner(
                            min_resource=1,
                            reduction_factor=4,
                            min_early_stopping_rate=0)

            elif pruner_method == 'none':
                pruner = None
            else:
                raise NotImplementedError("Pruner method %s is\
 not implemented." % pruner_method)

            # optuna study
            study = optuna.create_study(sampler=sampler,
                                        pruner=pruner,
                                        direction='maximize')
            self.study = study

        def objective(trial):
            kwargs = deepcopy(self.init_kwargs)

            # will raise exception if sample_parameters() is not
            # implemented by the agent class
            kwargs.update(self.agent_class.sample_parameters(trial))

            #
            # fit and evaluate agents
            #
            # Create AgentStats with hyperparams
            params_stats = AgentStats(
                self.agent_class,
                deepcopy(self.train_env),
                init_kwargs=kwargs,   # kwargs are being optimized
                fit_kwargs=deepcopy(self.fit_kwargs),
                policy_kwargs=deepcopy(self.policy_kwargs),
                agent_name='optim',
                n_fit=n_fit,
                n_jobs=n_jobs,
                verbose=0)

            # Fit and evaluate params_stats
            params_stats.fit()

            # Get rewards
            params_eval_env = deepcopy(self.eval_env)
            params_eval_env.reseed()

            eval_result = compare_policies(
                        [params_stats],
                        eval_env=params_eval_env,
                        eval_horizon=self.eval_horizon,
                        stationary_policy=True,
                        n_sim=n_sim,
                        plot=False)

            rewards = eval_result['optim'].values.mean()

            return rewards

        try:
            study.optimize(objective,
                           n_trials=n_trials,
                           n_jobs=n_jobs,
                           timeout=timeout)
        except KeyboardInterrupt:
            logging.warning("Evaluation stopped.")

        # continue
        best_trial = study.best_trial

        if self.verbose > 0:
            print('Number of finished trials: ', len(study.trials))

            print('Best trial:')

            print('Value: ', best_trial.value)

            print('Params: ')
            for key, value in best_trial.params.items():
                print('    {}: {}'.format(key, value))

        # update using best parameters
        self.init_kwargs.update(best_trial.params)

        return best_trial, study.trials_dataframe()


#
# Aux functions
#


def _fit_worker(args):
    agent_class, train_env, init_kwargs, fit_kwargs = args
    agent = agent_class(train_env, copy_env=False,
                        reseed_env=False, **init_kwargs)
    info = agent.fit(**fit_kwargs)
    return agent, info
