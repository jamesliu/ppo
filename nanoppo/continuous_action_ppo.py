import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.optim.lr_scheduler import CosineAnnealingLR, ExponentialLR
import torch.optim as optim
import gym
import numpy as np
import wandb
from tqdm import tqdm
import random
import os
import glob
from pathlib import Path

import click
from copy import deepcopy
from nanoppo.reward_scaler import RewardScaler
from nanoppo.reward_shaper import (
    RewardShaper,
    MountainCarDirectionalRewardShaper,
    TDRewardShaper,
)
from nanoppo.rollout_buffer import RolloutBuffer
from nanoppo.state_scaler import StateScaler
from nanoppo.metrics_recorder import MetricsRecorder
from nanoppo.network import PolicyNetwork, ValueNetwork
from nanoppo.random_utils import set_seed
from time import time
import warnings

# Suppress the specific warning
warnings.filterwarnings(
    "ignore",
    message="Could not parse CUBLAS_WORKSPACE_CONFIG, using default workspace size of 8519680 bytes.",
)

# SEED = 153 # Set a random seed for reproducibility MountainviewCar 43 Pendulum 153
# set_seed(SEED)


def save_checkpoint(policy, value, optimizer, epoch, checkpoint_path):
    # Create checkpoint directory if it does not exist
    if not os.path.exists(checkpoint_path):
        os.makedirs(checkpoint_path)
    checkpoint = {
        "epoch": epoch,
        "policy_state_dict": policy.state_dict(),
        "value_state_dict": value.state_dict(),
        "optimizer_state_dict": optimizer.state_dict(),
    }
    torch.save(checkpoint, f"{checkpoint_path}/checkpoint_epoch{epoch}.pt")


def load_checkpoint(policy, value, optimizer, checkpoint_path, epoch=None):
    # Find the latest checkpoint file
    if epoch is None:
        checkpoint_files = sorted(glob.glob(f"{checkpoint_path}/checkpoint_epoch*.pt"))
        if len(checkpoint_files) == 0:
            raise ValueError("No checkpoint found in the specified directory.")
        checkpoint_file = checkpoint_files[-1]
    else:
        checkpoint_file = f"{checkpoint_path}/checkpoint_epoch{epoch}.pt"

    checkpoint = torch.load(checkpoint_file)
    policy.load_state_dict(checkpoint["policy_state_dict"])
    value.load_state_dict(checkpoint["value_state_dict"])
    optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
    epoch = checkpoint["epoch"]
    return epoch


def setup_env(env_name, env_config):
    if env_config:
        env = gym.make(env_name, config=env_config)
    else:
        env = gym.make(env_name)
    return env


def setup_networks(
    env,
    optimizer_config,
    hidden_size,
    init_type,
    device,
):
    if isinstance(env.observation_space, gym.spaces.Dict):
        observation_space = env.observation_space.spaces["obs"]
    else:
        observation_space = env.observation_space
    if isinstance(env.action_space, gym.spaces.Box):
        action_dim = env.action_space.shape[0]
    else:
        action_dim = env.action_space.n

    policy = PolicyNetwork(
        observation_space.shape[0], action_dim, init_type=init_type
    ).to(device)
    value = ValueNetwork(
        observation_space.shape[0], hidden_size=hidden_size, init_type=init_type
    ).to(device)

    policy_lr = optimizer_config["policy_lr"]  # Add this to your config
    value_lr = optimizer_config["value_lr"]  # Add this to your config

    optimizer = optim.Adam(
        [
            {"params": policy.parameters(), "lr": policy_lr},
            {"params": value.parameters(), "lr": value_lr},
        ],
        betas=(optimizer_config["beta1"], optimizer_config["beta2"]),
        eps=optimizer_config["epsilon"],
        weight_decay=optimizer_config["weight_decay"],
    )

    if optimizer_config["scheduler"] is None:
        scheduler = None
    elif optimizer_config["scheduler"] == "exponential":
        scheduler = ExponentialLR(
            optimizer, gamma=optimizer_config["exponential_gamma"]
        )
    elif optimizer_config["scheduler"] == "cosine":
        # scheduler = CosineAnnealingLR(optimizer, T_max=epochs*sgd_iters)
        scheduler = CosineAnnealingLR(optimizer, T_max=optimizer_config["cosine_T_max"])
    else:
        raise ValueError(f"Scheduler {optimizer_config['scheduler']} not recognized.")
    return policy, value, optimizer, scheduler


def get_progress_iterator(last_epoch, epochs, verbose):
    if verbose:
        progress_iterator = tqdm(range(last_epoch, last_epoch + epochs))
    else:
        progress_iterator = range(last_epoch, last_epoch + epochs)
    return progress_iterator


def log_rewards(rewards):
    # Log the rewards during training
    wandb.log(
        {
            "Reward/Min": min(rewards),
            "Reward/Mean": sum(rewards) / len(rewards),
            "Reward/Max": max(rewards),
        }
    )


def rescale_action(predicted_action, action_min, action_max):
    return action_min + (predicted_action - (-1)) / 2 * (action_max - action_min)


def rollout_with_step(
    policy,
    value,
    env,
    device,
    rollout_buffer: RolloutBuffer,
    state_scaler: StateScaler,
    reward_shaper: RewardShaper,
    reward_scaler: RewardScaler,
    wandb_log: bool,
    debug: bool,
):
    total_steps = 0
    while True:
        steps = 0
        state, info = env.reset()
        if isinstance(state, dict):
            state = state["obs"]
        if state_scaler is None:
            scaled_state = state
        else:
            scaled_state = state_scaler.scale_state(state)
        done = False
        truncated = False
        accumulated_rewards = 0

        while (not done) and (not truncated):
            action, log_prob, action_mean, action_std = select_action(
                policy,
                scaled_state,
                device,
                env.action_space.low[0],
                env.action_space.high[0],
            )
            if wandb_log:
                wandb.log(
                    {
                        f"Policy/Action{i}_Mean": action_mean
                        for i, action_mean in enumerate(action_mean.numpy().tolist())
                    }
                )
                wandb.log(
                    {
                        f"Policy/Action{i}_Std": action_std
                        for i, action_std in enumerate(action_std.numpy().tolist())
                    }
                )

            action = action.numpy()
            log_prob = log_prob.numpy()

            next_state, reward, done, truncated, info = env.step(action)

            if isinstance(next_state, dict):
                next_state = next_state["obs"]

            if state_scaler is None:
                scaled_next_state = next_state
            else:
                scaled_next_state = state_scaler.scale_state(next_state)

            scaled_state = scaled_next_state
            accumulated_rewards += reward
            # Reshape rewards
            if reward_shaper is None:
                reshaped_reward = reward
            else:
                reshaped_reward = reward_shaper.reshape([reward], [state], [next_state])
            # Scale rewards
            if reward_scaler is None:
                scaled_reward = reshaped_reward
                # click.secho("Warning: Reward scaling is not applied.", fg="yellow", err=True)
            else:
                scaled_reward = reward_scaler.scale_rewards([reshaped_reward])[0]
            rollout_buffer.push(
                scaled_state,
                action.squeeze(),
                log_prob,
                scaled_reward,
                scaled_next_state,
                done,
            )
            total_steps += 1
            steps += 1
            if done or truncated:
                total_rewards = accumulated_rewards
                if debug:
                    print(
                        "total steps",
                        total_steps,
                        "steps",
                        steps,
                        "accumulated_rewards",
                        accumulated_rewards,
                        "done",
                        done,
                        "truncated",
                        truncated,
                        "reward",
                        reward,
                        "reshaped_reward",
                        reshaped_reward,
                        "scaled_reward",
                        scaled_reward,
                    )
                yield total_rewards, steps, total_steps
            else:
                yield None, steps, total_steps


def compute_gae(next_value, rewards, masks, values, gamma, tau):
    values = values + [next_value]
    gae = 0
    returns = []
    for step in reversed(range(len(rewards))):
        delta = rewards[step] + gamma * values[step + 1] * masks[step] - values[step]
        gae = delta + gamma * tau * masks[step] * gae
        returns.insert(0, gae + values[step])
    return returns


def surrogate(policy, old_probs, states, actions, advs, clip_param, entropy_coef):
    # Policy loss
    dist = policy(states)
    new_probs = dist.log_prob(actions).sum(-1)
    ratio = torch.exp(new_probs - old_probs)  # Importance sampling ratio
    surr1 = ratio * advs
    surr2 = (
        torch.clamp(ratio, 1 - clip_param, 1 + clip_param) * advs
    )  # Trust region clipping
    entropy = dist.entropy().mean()  # Compute the mean entropy of the distribution
    return (
        -torch.min(surr1, surr2).mean(),
        -entropy_coef * entropy,
    )  # Add the entropy term to the policy loss


def compute_value_loss(value, states, returns, l1_loss):
    # Compute value loss
    v_pred = value(states).squeeze()
    v_target = returns.squeeze()
    value_loss = (
        F.smooth_l1_loss(v_pred, v_target) if l1_loss else F.mse_loss(v_pred, v_target)
    )
    return value_loss


def select_action(policy: PolicyNetwork, state, device, action_min, action_max):
    state = torch.from_numpy(state).float().to(device)
    dist = policy(state)
    action = dist.sample()
    # If action space is continuous, compute the log_prob of the action, sum(-1) to sum over all dimensions
    log_prob = dist.log_prob(action).sum(-1)

    # Extract mean and std of the action distribution
    action_mean = dist.mean
    action_std = dist.stddev

    # action = torch.tanh(action)  # Pass the sampled action through the tanh activation function
    # action = action + torch.normal(mean=torch.zeros_like(action), std=action_std)  # Add noise to the action
    action = action.clamp(
        action_min, action_max
    )  # Clip the action to the valid range of the action space
    return (
        action.cpu().detach(),
        log_prob.cpu().detach(),
        action_mean.cpu().detach(),
        action_std.cpu().detach(),
    )


def train_networks(
    iter_num,
    policy,
    value,
    optimizer,
    scheduler,
    rollout_buffer,
    device,
    batch_size,
    sgd_iters,
    gamma,
    clip_param,
    vf_coef,
    entropy_coef,
    max_grad_norm,
    use_gae,
    tau,
    l1_loss,
    wandb_log,
    metrics_recorder: MetricsRecorder,
):
    assert (
        len(rollout_buffer) >= batch_size
    ), f"Rollout buffer length {len(rollout_buffer)} is less than batch size {batch_size}"
    for sgd_iter in range(sgd_iters):
        # Sample from the rollout buffer
        (
            batch_states,
            batch_actions,
            batch_probs,
            batch_rewards,
            batch_next_states,
            batch_dones,
        ) = rollout_buffer.sample(batch_size, device=device)

        # Compute Advantage and Returns
        returns = []
        advs = []
        g = 0
        # Compute returns and advantages from rollout buffer samples out of order
        with torch.no_grad():
            if use_gae:
                # Compute Advantage using GAE and Returns
                values = value(batch_states).squeeze().tolist()
                next_value = value(batch_next_states[-1]).item()
                masks = [1 - done.item() for done in batch_dones]
                returns = compute_gae(
                    next_value, batch_rewards, masks, values, gamma, tau
                )
                advs = [ret - val for ret, val in zip(returns, values)]
            else:
                for r, state, next_state, done in zip(
                    reversed(batch_rewards),
                    reversed(batch_states),
                    reversed(batch_next_states),
                    reversed(batch_dones),
                ):
                    mask = 1 - done.item()
                    next_value = value(next_state).item()
                    next_value = next_value * mask
                    returns.insert(0, g)
                    value_curr_state = value(state).item()
                    delta = r + gamma * next_value - value_curr_state
                    advs.insert(0, delta)
                    g = r + gamma * next_value * mask

        returns = torch.tensor(returns, dtype=torch.float32).to(device)
        advs = torch.tensor(advs, dtype=torch.float32).to(device)
        advs = (advs - advs.mean()) / (advs.std() + 1e-10)

        # Create mini-batches
        num_samples = len(batch_rewards)
        assert num_samples == batch_size
        num_batches = num_samples // batch_size
        assert num_batches == 1

        optimizer.zero_grad()
        policy_loss, entropy_loss = surrogate(
            policy,
            old_probs=batch_probs,
            states=batch_states,
            actions=batch_actions,
            advs=advs,
            clip_param=clip_param,
            entropy_coef=entropy_coef,
        )
        """
        # clear the list of activation norms for each epoch
        activation_norms = []
        def hook(module, input, output):
            if isinstance(output, Tuple):
                for o in output:
                    activation_norms.append(o.norm().item())
            else:
                activation_norms.append(output.norm().item())
        # register activation norm hook
        # add forward hook to each layer of the value network
        hooks = []
        for module in value.modules():
            hooks.append(module.register_forward_hook(hook))
        """

        value_loss = compute_value_loss(value, batch_states, returns, l1_loss)

        # Dynamically adjust vf_coef based on observed training dynamics
        loss_ratio = policy_loss.item() / (
            value_loss.item() + 1e-10
        )  # Adding a small epsilon to avoid division by zero
        # If policy loss is significantly larger, increase vf_coef
        if loss_ratio > 10:
            vf_coef *= 1.1
        # If value loss is significantly larger, decrease vf_coef
        if loss_ratio < 0.1:
            vf_coef *= 0.9
        # Limit vf_coef to a reasonable range to prevent it from becoming too large or too small
        vf_coef = min(max(vf_coef, 0.1), 10)

        # Compute total loss and update parameters
        total_loss = policy_loss + entropy_loss + vf_coef * value_loss

        total_loss.backward()

        # Clip the gradients to avoid exploding gradients
        policy_grad_norm = nn.utils.clip_grad_norm_(policy.parameters(), max_grad_norm)
        value_grad_norm = nn.utils.clip_grad_norm_(value.parameters(), max_grad_norm)

        # compute activation norm
        # remove the forward hooks
        """
        for hook in hooks:
            hook.remove()
        # compute the mean activation norm for the value network
        activation_norm = sum(activation_norms) / len(activation_norms)
        """

        optimizer.step()
        if scheduler is not None:
            scheduler.step()

        if wandb_log:
            # Log the losses and gradients to WandB
            wandb.log(
                {
                    "iteration": iter_num,
                    "Loss/Total": total_loss.item(),
                    "Loss/Policy": policy_loss.item(),
                    "Loss/Entropy": entropy_loss.item(),
                    "Loss/Value": value_loss.item(),
                    "Loss/Coef_Value": vf_coef * value_loss.item(),
                }
            )
            # wandb.log({"Gradients/PolicyNet": wandb.Histogram(policy.fc1.weight.grad.detach().cpu().numpy())})
            wandb.log(
                {
                    "Policy/Gradient_Norm": policy_grad_norm,
                    "Value/Gradient_Norm": value_grad_norm,
                    # "Value/Activation_Norm": activation_norm
                }
            )
            # wandb.log({"Gradients/ValueNet": wandb.Histogram(value.fc1.weight.grad.detach().cpu().numpy())})
            log_std_value = policy.log_std.detach().cpu().numpy()
            wandb.log({"Policy/Log_Std": log_std_value})
        # log the learning rate to wandb
        lrs = {}
        for i, param_group in enumerate(optimizer.param_groups):
            lr = param_group["lr"]
            lrs["learning_rate_{}".format(i)] = lr
            if wandb_log:
                wandb.log({"LR/LearningRate_{}".format(i): lr})

        if metrics_recorder:
            metrics_recorder.record_losses(
                total_loss.item(),
                policy_loss.item(),
                entropy_loss.item(),
                value_loss.item(),
            )
            metrics_recorder.record_learning(lrs)

        iter_num += 1
    return (
        policy,
        value,
        iter_num,
    )


def train(
    env_name: str,
    env_config: dict,
    shape_reward: RewardShaper,
    rescaling_rewards: bool,
    scale_states: str,
    epochs: int,
    batch_size: int,
    sgd_iters: int,
    gamma: float,
    optimizer_config: dict,
    hidden_size: int,
    init_type: str,
    clip_param: float,
    vf_coef: float,
    entropy_coef: float,
    max_grad_norm: float,
    use_gae: bool,
    tau: float,
    l1_loss: bool,
    wandb_log: bool,
    verbose: int,
    rollout_buffer_size: int,
    checkpoint_interval: int = -1,
    checkpoint_path: str = None,
    resume_training: bool = False,
    resume_epoch: bool = None,
    report_func: callable = None,
    project: str = "continuous-action-ppo",
    seed: int = None,
):
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    # Initialize WandB
    if wandb_log:
        config = locals().copy()
        keys_to_delete = [
            "checkpoint_interval",
            "checkpoint_path",
            "resume_training",
            "resume_epoch",
            "report_func",
        ]
        [config.pop(key, None) for key in keys_to_delete]
        wandb.init(project=project, name=env_name, config=config)
    metrics_recorder = MetricsRecorder()

    # Set up environment and neural networks
    env = setup_env(env_name, env_config)
    policy, value, optimizer, scheduler = setup_networks(
        env,
        optimizer_config,
        hidden_size=hidden_size,
        init_type=init_type,
        device=device,
    )
    rollout_buffer = RolloutBuffer(rollout_buffer_size)

    if rescaling_rewards:
        reward_scaler = RewardScaler()
    else:
        reward_scaler = None
        click.secho("No reward scaling", fg="red", err=True)

    if scale_states is None:
        state_scaler = None
        click.secho("No state scaling", fg="red", err=True)
    else:
        state_scaler = StateScaler(env, sample_size=10000, scale_type=scale_states)

    if resume_training:
        last_epoch = load_checkpoint(
            policy, value, optimizer, checkpoint_path, epoch=resume_epoch
        )
    else:
        last_epoch = 0

    if shape_reward is None:
        reward_shaper = None
    elif shape_reward == TDRewardShaper:
        reward_shaper = TDRewardShaper(model=value, device=device)
    else:
        reward_shaper = shape_reward()

    # Set up rollout generator
    rollout = rollout_with_step(
        policy,
        value,
        env,
        device,
        rollout_buffer,
        state_scaler,
        reward_shaper,
        reward_scaler,
        wandb_log,
        debug=True if verbose > 1 else False,
    )
    # Set up training loop
    episode_rewards = []
    episode_steps = []
    total_iters = epochs * sgd_iters
    train_iters = 0
    rollout_steps = 0
    average_reward = -np.inf
    start = time()
    for epoch in get_progress_iterator(last_epoch, epochs, verbose):
        policy.eval()
        value.eval()
        rollout_buffer.clear()  # clear the rollout buffer, all data is from the current policy
        for r in range(rollout_buffer_size):
            total_rewards, steps, rollout_steps = next(rollout)
            if total_rewards is not None:
                episode_steps.append(steps)
                episode_rewards.append(total_rewards)
        policy.train()
        value.train()
        _, _, train_iters = train_networks(
            train_iters,
            policy,
            value,
            optimizer,
            scheduler,
            rollout_buffer,
            device,
            batch_size,
            sgd_iters,
            gamma,
            clip_param,
            vf_coef,
            entropy_coef,
            max_grad_norm,
            use_gae=use_gae,
            tau=tau,
            l1_loss=l1_loss,
            wandb_log=wandb_log,
            metrics_recorder=metrics_recorder,
        )
        if checkpoint_interval > 0 and ((epoch + 1) % checkpoint_interval) == 0:
            save_checkpoint(policy, value, optimizer, epoch, checkpoint_path)

        # Average of last 10 episodes or all episodes if less than 10 episodes are available
        average_reward = sum(episode_rewards[-20:]) / len(episode_rewards[-20:])
        if len(episode_rewards) >= 20:
            if wandb_log:
                log_rewards(episode_rewards[-20:])
            if metrics_recorder:
                metrics_recorder.record_rewards(episode_rewards[-20:])
            if report_func:
                report_func(mean_reward=average_reward)  # Reporting the reward

        if verbose > 0:
            print(
                "epoch",
                epoch + 1,
                "average reward",
                average_reward,
                "train epochs",
                epoch - last_epoch + 1,
                "train iters",
                train_iters,
                "rollout episodes",
                len(episode_rewards),
                "rollout steps",
                rollout_steps,
            )
    end = time()
    print("Training time: ", round((end - start) / 60, 2), "minutes")

    metrics_recorder.to_csv()
    if wandb_log:
        wandb.finish()
    print(
        "Training complete",
        "average reward",
        average_reward,
        "total iters",
        total_iters,
    )
    return policy, value, average_reward, train_iters


config = {
    "env_name": "MountainCarContinuous-v0",
    "env_config": None,
    "seed": None,
    "epochs": 30,
    "rescaling_rewards": True,
    "shape_reward": None,  # [None, SubClass of RewardReshaper]
    "scale_states": "standard",  # [None, "env", "standard", "minmax", "robust", "quantile"]:
    "init_type": "he",  # xavier, he
    "use_gae": False,
    "tau": 0.97,
    "l1_loss": False,
    "rollout_buffer_size": 4096,
    "sgd_iters": 20,
    "hidden_size": 64,
    "batch_size": 64,
    "gamma": 0.99,
    "vf_coef": 1,
    "clip_param": 0.2,
    "max_grad_norm": 0.9,
    "entropy_coef": 1e-4,
    "wandb_log": True,
    "verbose": 2,
    "checkpoint_interval": 100,
}

optimizer_config = {
    "policy_lr": 3 * 1e-4,
    "value_lr": 3 * 1e-5,
    "beta1": 0.9,
    "beta2": 0.98,
    "epsilon": 1e-8,
    "weight_decay": 1e-4,
    "scheduler": "cosine",  # None, exponential, cosine
    "exponential_gamma": 0.9995,  # for exponential scheduler only
    "cosine_T_max": config["epochs"] * config["sgd_iters"],  # for cosine scheduler only
}


def update_config(aconfig):
    o = deepcopy(optimizer_config)
    c = deepcopy(config)
    for key in aconfig:
        if key in o:
            o[key] = aconfig[key]
        elif key in c:
            c[key] = aconfig[key]
        else:
            raise ValueError(f"Key {key} not recognized.")
    c.update(
        {
            "optimizer_config": o,
            "checkpoint_path": str(Path("checkpoints") / c["project"] / c["env_name"]),
        }
    )
    return c


if __name__ == "__main__":
    env_name = "MountainCarContinuous-v0"
    # env_name = 'Pendulum-v1'

    if env_name == "MountainCarContinuous-v0":
        best_config = (
            {}
        )  # {'env_name': 'MountainCarContinuous-v0', 'policy_lr': 1.3854471971413998e-06, 'value_lr': 4.324566907389423e-05, 'weight_decay': 4.628061893014567e-05, 'scheduler': None, 'sgd_iters': 10, 'batch_size': 64, 'clip_param': 0.2190924577076417, 'max_grad_norm': 0.5806241298866155, 'vf_coef': 0.8005184316885099, 'entropy_coef': 6.504618115019088e-06, 'tau': 0.9020595524919125}
        best_config = {
            "env_name": "MountainCarContinuous-v0",
            "epochs": 30,
            "rescaling_rewards": False,
            "shape_reward": TDRewardShaper,
            "policy_lr": 2.2139290514335154e-04,
            "value_lr": 6.717375374452914e-04,
            "weight_decay": 2.5128866121352096e-06,
            "scheduler": None,
            "sgd_iters": 10,
            "batch_size": 128,
            "clip_param": 0.2622072783993743,
            "max_grad_norm": 0.537370676793494,
            "vf_coef": 0.5225264301194332,
            "entropy_coef": 6.861142534298038e-05,
            "tau": 0.9582092793934365,
        }
    elif env_name == "Pendulum-v1":
        # best_config = {'epochs':100}
        best_config = {
            "epochs": 30,
            "env_name": "Pendulum-v1",
            "policy_lr": 5.8281040558151076e-05,
            "value_lr": 0.0001120576307122378,
            "weight_decay": 0.0008145726123243288,
            "sgd_iters": 2,
            "rollout_buffer_size": 512,
            "use_gae": False,
            "init_type": "he",
            "batch_size": 512,
            "clip_param": 0.21698505583910346,
            "max_grad_norm": 0.5552556781050277,
            "vf_coef": 1.90491891614271956,
            "entropy_coef": 3.015475350516561e-05,
            "tau": 0.9552356381259465,
        }
    best_config["env_name"] = env_name
    train_config = update_config(best_config)
    print("train config", train_config)
    set_seed(train_config.pop("seed", None))
    policy, value, average_reward, total_iters = train(**train_config)
    print("train", "average reward", average_reward, "total iters", total_iters)
