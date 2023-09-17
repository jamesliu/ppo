import click
from nanoppo.ppo_agent import PPOAgent
from nanoppo.reward_shaper import TDRewardShaper, MountainCarAdvancedRewardShaper

@click.command()
@click.option('--project', default="continuous-action-ppo", help='Name of the project.')
@click.option('--env_name', default="MountainCarContinuous-v0", help='Name of the environment.', 
              type= click.Choice(["Pendulum-v1", "MountainCarContinuous-v0"]))
@click.option('--epochs', default=30, help='Number of training epochs.')
@click.option('--rescaling_rewards', is_flag=True, default=True, help='Flag to rescale rewards.')
@click.option('--scale_states', default="standard", help='Type of state scaling.')
@click.option('--batch_size', default=64, help='Batch size for training.')
@click.option('--sgd_iters', default=10, help='Number of SGD iterations.')
@click.option('--gamma', default=0.99, help='Discount factor.')
@click.option('--hidden_size', default=64, help='Hidden size for the neural network.')
@click.option('--init_type', default="xavier_uniform", help='Initialization type for neural network weights.')
@click.option('--clip_param', default=0.2, help='Clipping parameter for PPO.')
@click.option('--vf_coef', default=0.5, help='Value function coefficient.')
@click.option('--entropy_coef', default=0.01, help='Entropy coefficient.')
@click.option('--max_grad_norm', default=0.5, help='Maximum gradient norm for clipping.')
@click.option('--rollout_buffer_size', default=2000, help='Size of the rollout buffer.')
@click.option('--wandb_log', is_flag=True, default=True, help='Flag to log results to wandb.')
@click.option('--metrics_log', is_flag=True, default=False, help='Flag to log results to csv.')
@click.option('--verbose', default=2, help='Verbosity level.')
@click.option('--checkpoint_interval', default=100, help='Checkpoint interval.')
@click.option('--checkpoint_dir', default='.', help='Path to checkpoint.')
@click.option('--resume_training', is_flag=True, default=False, help='Flag to resume training.')
@click.option('--resume_epoch', default=None, help='Epoch to resume training from.')
@click.option('--verbose', default=2, help='Verbosity level.')
# Add more configurations as needed

def cli(project, env_name, epochs, rescaling_rewards, scale_states,
        batch_size, sgd_iters, gamma, hidden_size, init_type, 
        clip_param, vf_coef, entropy_coef, max_grad_norm, rollout_buffer_size, wandb_log, metrics_log, 
        checkpoint_interval, checkpoint_dir, resume_training, resume_epoch, verbose):
    # Configuration for the environment and the agent
    config = {
        "project": project,  # Name of the project
        "env_name": env_name,
        "env_config": None,
        "seed": None,
        "epochs": epochs,
        "rescaling_rewards": rescaling_rewards,
        "shape_reward": None, # [None, SubClass of RewardReshaper]
        "scale_states": scale_states,  # [None, "env", "standard", "minmax", "robust", "quantile"]:
        "init_type": "he",  # xavier, he
        "use_gae": False,
        "tau": 0.97,
        "l1_loss": False,
        "rollout_buffer_size": rollout_buffer_size,
        "sgd_iters": 20,
        "hidden_size": hidden_size,
        "batch_size": 64,
        "gamma": 0.99,
        "vf_coef": 1,
        "clip_param": 0.2,
        "max_grad_norm": 0.9,
        "entropy_coef": 1e-4,
        "wandb_log": wandb_log,
        "metrics_log": metrics_log,
        "verbose": 2,
        "checkpoint_interval": checkpoint_interval,
        "checkpoint_dir": checkpoint_dir,
        "resume_training": resume_training,
        "resume_epoch": resume_epoch,
        "report_func": None
    }
    optimizer_config = {
        "policy_lr": 3e-4,
        "value_lr": 3e-4,
        "beta1": 0.9,
        "beta2": 0.999,
        "epsilon": 1e-8,
        "weight_decay": 0,
        "scheduler": "cosine",
        "cosine_T_max": config['epochs'] * config['sgd_iters'],  # arbitrary value; you might want to adjust
        "exponential_gamma": 0.99  # arbitrary value; you might want to adjust
    }

    agent = PPOAgent(config, optimizer_config)
    agent.train(epochs=epochs)

if __name__ == "__main__":
    cli()
