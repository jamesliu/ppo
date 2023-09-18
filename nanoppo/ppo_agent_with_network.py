import torch
import torch.nn as nn
import torch.optim as optim

# Neural Network for Actor-Critic
import torch.nn as nn
import torch.nn.functional as F

class ActorCritic(nn.Module):
    def __init__(self, state_dim, action_dim, n_latent_var, action_low, action_high):
        super(ActorCritic, self).__init__()
        self.action_low = action_low
        self.action_high = action_high

        # Actor: outputs mean and log standard deviation
        self.action_mu = nn.Sequential(
            nn.Linear(state_dim, n_latent_var),
            nn.Tanh(),
            nn.Linear(n_latent_var, n_latent_var),
            nn.Tanh(),
            nn.Linear(n_latent_var, action_dim)
        )

        self.action_log_std = nn.Sequential(
            nn.Linear(state_dim, n_latent_var),
            nn.Tanh(),
            nn.Linear(n_latent_var, n_latent_var),
            nn.Tanh(),
            nn.Linear(n_latent_var, action_dim)
        )

        # Critic
        self.value_layer = nn.Sequential(
            nn.Linear(state_dim, n_latent_var),
            nn.Tanh(),
            nn.Linear(n_latent_var, n_latent_var),
            nn.Tanh(),
            nn.Linear(n_latent_var, 1)
        )

    def forward(self):
        raise NotImplementedError

    def act(self, state, action=None, compute_logprobs=True):
        mu = self.action_mu(state)
        log_std = self.action_log_std(state)
        std = log_std.exp()

        dist = torch.distributions.Normal(mu, std)
        if action is None:
            action = dist.sample()
            # Assuming env.action_space.low and env.action_space.high are numpy arrays
            action_low_tensor = torch.tensor(self.action_low, dtype=torch.float32)
            action_high_tensor = torch.tensor(self.action_high, dtype=torch.float32)
            #action = torch.clamp(action, action_low_tensor, action_high_tensor)

        if compute_logprobs:
            logprobs = dist.log_prob(action).sum(axis=-1)
            return action, logprobs
        
        return action

    def evaluate(self, state, action):
        mu = self.action_mu(state)
        log_std = self.action_log_std(state)
        std = log_std.exp()
        
        dist = torch.distributions.Normal(mu, std)
        logprobs = dist.log_prob(action).sum(axis=-1)
        state_value = self.value_layer(state)
        
        return logprobs, torch.squeeze(state_value)

    def get_value(self, state):
        return self.value_layer(state)

# PPO Agent
class PPOAgent:
    def __init__(self, state_dim, action_dim, n_latent_var, lr, betas, gamma, K_epochs, eps_clip, state_normalizer, action_low, action_high):
        self.gamma = gamma
        self.eps_clip = eps_clip
        self.K_epochs = K_epochs
        self.state_normalizer = state_normalizer
        self.action_low = action_low
        self.action_high = action_high

        self.policy = ActorCritic(state_dim, action_dim, n_latent_var, action_low, action_high).float()
        self.policy_old = ActorCritic(state_dim, action_dim, n_latent_var, action_low, action_high).float()
        self.policy_old.load_state_dict(self.policy.state_dict())

        self.optimizer = torch.optim.Adam(self.policy.parameters(), lr=lr, betas=betas)
        self.mse_loss = nn.MSELoss()

    def update(self, states, actions, returns, next_states, dones):
        for _ in range(self.K_epochs):
            # Getting predicted values and log probs for given states and actions
            logprobs, state_values = self.policy.evaluate(states, actions)
            # Calculate the advantages
            advantages = returns - state_values.detach()
    
            # Normalize the advantages (optional, but can help in training stability)
            advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-5)
    
            # Compute ratio for PPO
            old_logprobs, _ = self.policy_old.evaluate(states, actions)
            ratio = torch.exp(logprobs - old_logprobs.detach())
    
            # Surrogate loss
            surr1 = ratio * advantages
            surr2 = torch.clamp(ratio, 1-self.eps_clip, 1+self.eps_clip) * advantages
            policy_loss = -torch.min(surr1, surr2).mean()
    
            # Value loss
            value_loss = 0.5 * self.mse_loss(state_values, returns)
    
            # Entropy (for exploration)
            entropy_loss = -0.01 * logprobs.mean()
    
            # Total loss
            loss = policy_loss + value_loss + entropy_loss
            
            # Optimize policy network
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()
            
        # Copy new weights into old policy
        self.policy_old.load_state_dict(self.policy.state_dict())

    def save(self, path):
        # Saving
        torch.save({
            'model_state_dict': self.policy.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'state_normalizer_state': self.state_normalizer.get_state(),
        }, path)
        
    def load(self, path):
        checkpoint = torch.load(path)
        self.policy.load_state_dict(checkpoint['model_state_dict'])
        self.policy_old.load_state_dict(self.policy.state_dict())
        self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        self.state_normalizer.set_state(checkpoint['state_normalizer_state'])

