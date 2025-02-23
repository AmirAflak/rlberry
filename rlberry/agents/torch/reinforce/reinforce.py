import inspect

import gymnasium.spaces as spaces
import numpy as np
import torch

import rlberry
from rlberry.agents import AgentTorch, AgentWithSimplePolicy
from rlberry.agents.torch.utils.models import default_policy_net_fn
from rlberry.agents.torch.utils.training import optimizer_factory
from rlberry.agents.utils.memories import Memory
from rlberry.utils.torch import choose_device

logger = rlberry.logger


class REINFORCEAgent(AgentTorch, AgentWithSimplePolicy):
    """
    REINFORCE with entropy regularization.

    Parameters
    ----------
    env : Model
        Online model with continuous (Box) state space and discrete actions
    batch_size : int, default: 8
        Number of episodes used for the update of the policy netowrk.
    horizon : int, default: 256
        Episode length: one transition per episode steps. So total number of transitions used for one policy update is batch_size * horizon.
    gamma : double
        Discount factor in [0, 1].
    entr_coef : double
        Entropy coefficient.
    learning_rate : double
        Learning rate.
    normalize: bool
        If True normalize rewards
    optimizer_type: str
        Type of optimizer. 'ADAM' by defaut.
    policy_net_fn : function(env, **kwargs)
        Function that returns an instance of a policy network (pytorch).
        If None, a default net is used.
    policy_net_kwargs : dict
        kwargs for policy_net_fn
    use_bonus_if_available : bool, default = False
        If true, check if environment info has entry 'exploration_bonus'
        and add it to the reward. See also UncertaintyEstimatorWrapper.
    device: str
        Device to put the tensors on

    Attributes
    ----------
    device : str
        Torch device on which the agent's neural networks are placed.
    batch_size : int, default: 8
        Number of episodes used for the update of the policy netowrk.
    horizon : int, default: 256
        Episode length: one transition per episode steps.
    gamma : float, default: 0.99
        Discount factor used to discount future rewards in the Bellman equation.
    state_dim : int
        Dimensionality of the continuous state space of the environment.
    action_dim : int
        Number of discrete actions available in the environment.
    policy_net_fn : function(env, **kwargs)
        Function that returns an instance of a policy network (PyTorch).
    policy_net_kwargs : dict
        Keyword arguments for `policy_net_fn`.
    optimizer_kwargs : dict
        Keyword arguments for the optimizer used during neural network training.
    policy_net : torch.nn.Module
        The policy network used by the agent.
    policy_optimizer : torch.optim.Optimizer
        The optimizer used for training the policy network.
    memory : Memory
        The memory buffer used to store the agent's experiences.
    episode : int
        A counter that keeps track of the number of episodes.

    References
    ----------
    Williams, Ronald J.,
    "Simple statistical gradient-following algorithms for connectionist
    reinforcement learning."
    ReinforcementLearning.Springer,Boston,MA,1992.5-3
    """

    name = "REINFORCE"

    def __init__(
        self,
        env,
        batch_size=8,
        horizon=256,
        gamma=0.99,
        entr_coef=0.01,
        learning_rate=0.0001,
        normalize=True,
        optimizer_type="ADAM",
        policy_net_fn=None,
        policy_net_kwargs=None,
        use_bonus_if_available=False,
        device="cuda:best",
        **kwargs
    ):
        # For all parameters, define self.param = param
        _, _, _, values = inspect.getargvalues(inspect.currentframe())
        values.pop("self")
        for arg, val in values.items():
            setattr(self, arg, val)

        AgentWithSimplePolicy.__init__(self, env, **kwargs)

        self.device = choose_device(device)

        self.state_dim = self.env.observation_space.shape[0]
        self.action_dim = self.env.action_space.n

        self.policy_net_kwargs = policy_net_kwargs or {}

        #
        self.policy_net_fn = policy_net_fn or default_policy_net_fn

        self.optimizer_kwargs = {"optimizer_type": optimizer_type, "lr": learning_rate}

        # check environment
        assert isinstance(self.env.observation_space, spaces.Box)
        assert isinstance(self.env.action_space, spaces.Discrete)

        self.policy_net = None  # policy network

        # initialize
        self.reset()

    def reset(self, **kwargs):
        self.policy_net = self.policy_net_fn(self.env, **self.policy_net_kwargs).to(
            self.device
        )

        self.policy_optimizer = optimizer_factory(
            self.policy_net.parameters(), **self.optimizer_kwargs
        )

        self.memory = Memory()

        self.episode = 0

    def policy(self, observation):
        state = observation
        assert self.policy_net is not None
        state = torch.from_numpy(state).float().to(self.device)
        action_dist = self.policy_net(state)
        action = action_dist.sample().item()
        return action

    def fit(self, budget: int, **kwargs):
        """
        Train the agent using the provided environment.

        Parameters
        ----------
        budget: int
            number of episodes. Each episode runs for self.horizon unless it
            enconters a terminal state in which case it stops early.
        """
        del kwargs
        n_episodes_to_run = budget
        count = 0
        while count < n_episodes_to_run:
            self._run_episode()
            count += 1

    def _run_episode(self):
        # interact for H steps
        episode_rewards = 0
        observation, info = self.env.reset()
        for _ in range(self.horizon):
            # running policy
            action = self.policy(observation)
            next_observation, reward, terminated, truncated, info = self.env.step(
                action
            )
            done = terminated or truncated

            # check whether to use bonus
            bonus = 0.0
            if self.use_bonus_if_available:
                if info is not None and "exploration_bonus" in info:
                    bonus = info["exploration_bonus"]

            # save in batch
            self.memory.states.append(observation)
            self.memory.actions.append(action)
            self.memory.rewards.append(reward + bonus)  # add bonus here
            self.memory.is_terminals.append(done)
            episode_rewards += reward

            if done:
                break

            # update observation
            observation = next_observation

        # update
        self.episode += 1

        #
        if self.writer is not None:
            self.writer.add_scalar("episode_rewards", episode_rewards, self.episode)

        #
        if self.episode % self.batch_size == 0:
            self._update()
            self.memory.clear_memory()

        return episode_rewards

    def _normalize(self, x):
        return (x - x.mean()) / (x.std() + 1e-5)

    def _update(self):
        # monte carlo estimate of rewards
        rewards = []
        discounted_reward = 0
        for reward, is_terminal in zip(
            reversed(self.memory.rewards), reversed(self.memory.is_terminals)
        ):
            if is_terminal:
                discounted_reward = 0
            discounted_reward = reward + (self.gamma * discounted_reward)
            rewards.insert(0, discounted_reward)

        # convert list to tensor
        states = torch.FloatTensor(np.array(self.memory.states)).to(self.device)
        actions = torch.LongTensor(self.memory.actions).to(self.device)
        rewards = torch.FloatTensor(rewards).to(self.device)
        if self.normalize:
            rewards = self._normalize(rewards)

        # evaluate logprobs
        action_dist = self.policy_net(states)
        logprobs = action_dist.log_prob(actions)
        dist_entropy = action_dist.entropy()

        # compute loss
        loss = -logprobs * rewards - self.entr_coef * dist_entropy

        # take gradient step
        self.policy_optimizer.zero_grad()

        loss.mean().backward()

        self.policy_optimizer.step()

    #
    # For hyperparameter optimization
    #
    @classmethod
    def sample_parameters(cls, trial):
        batch_size = trial.suggest_categorical("batch_size", [1, 4, 8, 16, 32])
        gamma = trial.suggest_categorical("gamma", [0.9, 0.95, 0.99])
        learning_rate = trial.suggest_float("learning_rate", 1e-5, 1, log=True)

        entr_coef = trial.suggest_float("entr_coef", 1e-8, 0.1, log=True)

        return {
            "batch_size": batch_size,
            "gamma": gamma,
            "learning_rate": learning_rate,
            "entr_coef": entr_coef,
        }
