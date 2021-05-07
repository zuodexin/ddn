#!/usr/bin/env python
#
# ROBUST VECTOR POOLING
# Implements robust pooling of vector arguments by solving,
#
#    minimize \sum_{i=1}^{n} \phi(\|u - x_i\|_2; \alpha)
#
# over u in R^m, where \phi is a (robust) penalty function, one of:
#   quadratic
#   pseudo-huber
#   huber
#   welsch
#   truncated quadratic
#
# See accompanying DDN documentation at https://deepdeclarativenetworks.com.
#
# Stephen Gould <stephen.gould@anu.edu.au>
# Dylan Campbell <dylan.campbell@anu.edu.au>
# Yizhak Ben Shabat <Yizhak.BenShabat@anu.edu.au>
#

import torch
import torch.nn as nn

# --- Penalty functions -------------------------------------------------------

class Penalty():
    """
    Prototype for a penalty function, phi, including methods necessary for optimizing and back-propagation.
    See `Quadratic` for an example.
    """
    is_convex = None

    @staticmethod
    def phi(z, alpha):
        """Evaluates the penality function on a batch of inputs."""
        raise NotImplementedError()

    @staticmethod
    def kappa(z, alpha):
        """Evaluates phi'/z and phi'' - phi'/z."""
        raise NotImplementedError()


class Quadratic(Penalty):
    """Quadratic penality function. Don't use for anything other than testing."""
    is_convex = True

    @staticmethod
    def phi(z, alpha=1.0):
        return 0.5 * torch.pow(z, 2.0)

    @staticmethod
    def kappa(z, alpha=1.0):
        return torch.ones_like(z), torch.zeros_like(z)


class PseudoHuber(Penalty):
    """Pseudo-Huber penalty."""
    is_convex = True

    @staticmethod
    def phi(z, alpha=1.0):
        alpha2 = alpha * alpha
        return alpha2 * (torch.sqrt(1.0 + torch.pow(z, 2.0) / alpha2) - 1.0)

    @staticmethod
    def kappa(z, alpha=1.0):
        z2 = torch.pow(z, 2.0)/ (alpha * alpha)
        xi = torch.sqrt(1.0 + z2)
        return 1.0 / xi, -1.0 * z2 / torch.pow(xi, 3.0)


class Huber(Penalty):
    """Huber penalty."""
    is_convex = True

    @staticmethod
    def phi(z, alpha=1.0):
        alpha2 = alpha * alpha
        return torch.where(torch.abs(z) <= alpha, 0.5 * torch.pow(z, 2.0), alpha * torch.abs(z) - 0.5 * alpha2)

    @staticmethod
    def kappa(z, alpha=1.0):
        indx = torch.abs(z) <= alpha
        alpha_on_z = alpha / torch.abs(z)
        return torch.where(indx, torch.ones_like(z), alpha_on_z), torch.where(indx, torch.zeros_like(z), 0.0 - alpha_on_z)


class Welsch(Penalty):
    """Welsch penalty."""
    is_convex = False

    @staticmethod
    def phi(z, alpha=1.0):
        alpha2 = alpha * alpha
        return 1.0 - torch.exp(-0.5 * torch.pow(z, 2.0) / alpha2)

    @staticmethod
    def kappa(z, alpha=1.0):
        alpha2 = alpha * alpha
        z2 = torch.pow(z, 2.0)
        xi = torch.exp(-0.5 * z2 / alpha2)
        return xi / alpha2, -1.0 * z2 * xi / (alpha2 * alpha2)


class TruncQuad(Penalty):
    """Truncated quadratic penalty."""
    is_convex = False

    @staticmethod
    def phi(z, alpha=1.0):
        return torch.minimum(0.5 * torch.pow(z, 2.0), torch.full_like(z, 0.5 * alpha * alpha))

    @staticmethod
    def kappa(z, alpha=1.0):
        indx = torch.abs(z) <= alpha
        return torch.where(indx, torch.ones_like(z), torch.zeros_like(z)), torch.zeros_like(z)


# --- PyTorch Function --------------------------------------------------------

class RobustVectorPool2dFcn(torch.autograd.Function):
    """PyTorch autograd function for robust vector pooling. Input (B,C,H,W) -> output (B,C)."""

    @staticmethod
    def _optimize(x, y, penalty, alpha):
        B, C, H, W = x.shape
        y = y.clone()
        y.requires_grad = True
        opt = torch.optim.LBFGS([y], lr=1.0, max_iter=100, max_eval=None,
            tolerance_grad=1e-05, tolerance_change=1e-09, history_size=5, line_search_fn=None)

        def reevaluate():
            opt.zero_grad()
            z = torch.linalg.norm((y.view(B, C, 1, 1) - x), dim=1)
            f = penalty.phi(z, alpha).sum()
            f.backward()
            return f

        # optimize to convergence (unlike step for other optimizers)
        opt.step(reevaluate)

        return y


    @staticmethod
    def forward(ctx, x, penalty, alpha=1.0, restarts=0):
        assert alpha > 0.0
        assert restarts >= 0

        B, C, H, W = x.shape
        with torch.no_grad():
            if penalty.is_convex:
                y = torch.mean(x, dim=(2, 3))
                y = RobustVectorPool2dFcn._optimize(x, y, penalty, alpha)
            else:
                y_mean = torch.mean(x, dim=(2, 3))
                y = RobustVectorPool2dFcn._optimize(x, y_mean, penalty, alpha)
                f = penalty.phi(torch.linalg.norm((y.view(B, C, 1, 1) - x), dim=1), alpha).view(B, -1).sum(-1)

                y_median, _ = torch.median(x.view(B, C, -1), dim=2)
                y_median = RobustVectorPool2dFcn._optimize(x, y_median, penalty, alpha)
                f_median = penalty.phi(torch.linalg.norm((y_median.view(B, C, 1, 1) - x), dim=1), alpha).view(B, -1).sum(-1)
                y = torch.where((f <= f_median).view(B, 1), y, y_median)
                f = torch.minimum(f, f_median)

                if restarts > 0:
                    guesses = torch.randperm(H * W)
                    if H * W > restarts:
                        guesses = guesses[0:restarts]
                    for i in guesses:
                        y_init = x.view(B, C, -1)[:, :, i]
                        y_final = RobustVectorPool2dFcn._optimize(x, y_init, penalty, alpha)
                        f_final = penalty.phi(torch.linalg.norm((y_final.view(B, C, 1, 1) - x), dim=1), alpha).view(B, -1).sum(-1)
                        y = torch.where((f < f_final).view(B, 1), y, y_final)
                        f = torch.minimum(f, f_final)

        ctx.save_for_backward(x, y)
        ctx.penalty = penalty
        ctx.alpha = alpha

        return y

    @staticmethod
    def backward(ctx, y_grad):

        x, y = ctx.saved_tensors
        B, C, H, W = x.shape

        x_minus_y = y.view(B, C, 1, 1) - x
        z = torch.linalg.norm(x_minus_y, dim=1, keepdim=True)
        x_minus_y_on_z = (x_minus_y / z).view(B, C, -1)

        k1, k2 = ctx.penalty.kappa(z, ctx.alpha)
        HH = torch.sum(k1.view(B, -1), dim=1).view(B, 1, 1) * torch.eye(C, dtype=x.dtype, device=x.device).view(1, C, C) + \
            torch.einsum("bik,bjk->bij", x_minus_y_on_z, k2.view(B, 1, -1) * x_minus_y_on_z)
        L = torch.cholesky(HH)
        v = torch.cholesky_solve(y_grad.view(B, C, -1), L).view(B, C)

        w = torch.einsum("bi,bik->bk", v, k2.view(B, 1, -1) * x_minus_y_on_z)
        x_grad = k1 * v.view(B, C, 1, 1) + torch.einsum("bk,bik->bik", w, x_minus_y_on_z).view(B, C, H, W)

        return x_grad, None, None


# --- PyTorch Layer -----------------------------------------------------------

class RobustVectorPool2d(nn.Module):

    def __init__(self, penalty, alpha=1.0, restarts=10):
        super(RobustVectorPool2d, self).__init__()
        self.penalty = penalty
        self.alpha = alpha
        self.restarts = restarts

    def forward(self, x):
        return RobustVectorPool2dFcn.apply(x, self.penalty, self.alpha, self.restarts)


# --- Testing -----------------------------------------------------------------

if __name__ == '__main__':

    from torch.autograd import gradcheck

    torch.manual_seed(0)
    x = torch.randn((2, 3, 5, 5), dtype=torch.double)
    print(torch.mean(x, dim=(2, 3)))

    # add an outlier
    x[:, :, 0, 0] = 10.0 * x[:, :, 0, 0]
    print(torch.mean(x, dim=(2, 3)))

    # evaluate function
    f = RobustVectorPool2dFcn().apply
    penalties = [Quadratic(), PseudoHuber(), Huber(), Welsch(), TruncQuad()]
    for p in penalties:
        y = f(x, p, 1.0)
        print("{}: {}".format(p.__class__.__name__, y))

    # evaluate gradient
    x.requires_grad = True
    for p in penalties:
        test = gradcheck(f, (x, p), eps=1e-6, atol=1e-3, rtol=1e-6)
        print("{}: {}".format(p.__class__.__name__, test))

