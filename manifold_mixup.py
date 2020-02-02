"Implements a fastai callback for the [Manifold Mixup](http://proceedings.mlr.press/v97/verma19a/verma19a.pdf) training method."
from fastai.torch_core import *
from fastai.callback import *
from fastai.basic_train import Learner, LearnerCallback
from fastai.text import models

__all__ = ["ManifoldMixupModule", "ManifoldMixupLoss", "ManifoldMixupCallback", "manifold_mixup", "non_mixable_module_types"]

def _adapt_dim(t, t_target):
    """
    Takes a tensor and adds trailing dimensions until it fits the dimension of the target tensor
    This function is useful to multiply tensors of arbitrary size
    implementation inspired by: https://github.com/pytorch/pytorch/issues/9410#issuecomment-552786888
    """
    # this might be implementable with `view` (?)
    nb_current_dim = t.dim()
    nb_target_dim = t_target.dim()
    t = t[(..., )*nb_current_dim + (None, ) * (nb_target_dim-nb_current_dim)]
    return t

# classes of modules that should be avoided when using mixup
non_mixable_module_types = [nn.Sequential, nn.Dropout, nn.Dropout2d, nn.Dropout3d, nn.AlphaDropout,
                            nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d, nn.SyncBatchNorm,
                            nn.LSTM, nn.LSTMCell, nn.GRU, nn.GRUCell, models.AWD_LSTM,
                            nn.RNN, nn.RNNBase, nn.RNNCell, nn.RNNCellBase]

def _is_mixable(m):
    "Checks wether the module m is an instance of a module that is allowed for mixup."
    return not any(isinstance(m, non_mixable_class) for non_mixable_class in non_mixable_module_types)

class ManifoldMixupModule(Module):
    """
    Wrap a module with this class to indicate that you wish to apply manifold mixup to the output of this module.
    Note that this, by itself, has no effect and is just used to locate modules of interest when using the ManifoldMixupCallback.
    """
    def __init__(self, module):
        super(ManifoldMixupModule, self).__init__()
        self.module = module

    def forward(self, x, *args, **kwargs):
        return self.module(x, *args, **kwargs)

def _get_module_list(model, use_only_mixup_modules):
    "returns all the modules that can be used for mixup"
    if use_only_mixup_modules:
        module_list = list(filter(lambda module: isinstance(module, ManifoldMixupModule), list(model.modules())))
    else:
        module_list = list(filter(_is_mixable, list(model.modules())))
    if len(module_list) == 0:
        raise ValueError('No eligible layer found for mixup. Try passing use_only_mixup_modules=False or wrap one of your modules with a ManifoldMixupModule')
    print(f'{len(module_list)} modules eligible for mixup')
    return module_list

class ManifoldMixupCallback(LearnerCallback):
    "Callback that creates the mixed-up input and target."
    def __init__(self, learn:Learner, alpha:float=0.4, use_symmetric_batch:bool=True, use_input_mixup:bool=True, use_only_mixup_modules:bool=False):
        """
        `alpha` is the parameter for the beta law.

        If `use_symmetric_batch` is set to true, each mixed batch element will produce two outputs in order to avoid wasted computation.
        The outputs will be produced by the combinaisons `lam*x1 + (1-lam)*x2` and `lam*x2 + (1-lam)*x1`.

        If `use_input_mixup` is set to True, mixup might also be applied to the inputs.

        If `use_only_mixup_modules` is set to false, mixup will be applied to a random valid module.
        Oherwise it will only be applied to a ManifoldMixupModule.
        """
        super().__init__(learn)
        # parameters describing the mixup
        self.alpha = alpha
        self.use_only_mixup_modules = use_only_mixup_modules
        self.use_input_mixup = use_input_mixup
        self.use_symmetric_batch = use_symmetric_batch
        # modules on which we may apply mixup
        self.module_list = _get_module_list(learn.model, use_only_mixup_modules)
        # temporary variables storing intermediate states
        self.lam = None
        self.input = None
        self.intermediate_output1 = None
        self.intermediate_output2 = None
        self.output = None
        self.mixup_hook = None
        self.is_input_mixup = None
        self._warning_raised = False

    def on_train_begin(self, **kwargs):
        "Injects ManifoldMixupLoss on top of the current loss function."
        self.learn.loss_func = ManifoldMixupLoss(self.learn.loss_func)

    def on_batch_begin(self, last_input, last_target, train, **kwargs):
        "Selects a module to apply mixup and modifies the target accordingly."
        if not train: return
        # creates tensor filled with the random ponderation drawn from a beta distribution of parameter alpha
        lambd = np.random.beta(self.alpha, self.alpha, last_target.size(0))
        lambd = np.concatenate([lambd[:,None], 1-lambd[:,None]], 1).max(1)
        self.lam = torch.from_numpy(lambd).float().to(last_input.device)
        # decides on a way to shuffle inputs
        shuffle = torch.randperm(last_target.size(0)).to(last_input.device)
        last_target2 = last_target[shuffle]
        output_lam = self.lam
        # selects a module to apply mixup
        minimum_module_index = -1 if self.use_input_mixup else 0
        k = np.random.randint(minimum_module_index, len(self.module_list))
        if k == -1: # applies mixup to an input
            self.is_input_mixup = True
            input_lam = _adapt_dim(self.lam, last_input)
            last_input = (1 - input_lam) * last_input + input_lam * last_input[shuffle]
        else: # applies mixup to an inner module
            self.is_input_mixup = False
            self.input = last_input[shuffle]
            self.mixup_hook = self.module_list[k].register_forward_hook(self.hook_mixup)
            if self.use_symmetric_batch: # stacks targets to use both mixed outputs
                last_target, last_target2 = torch.cat((last_target, last_target2), dim=0), torch.cat((last_target2, last_target), dim=0)
                output_lam = torch.cat((output_lam, output_lam), dim=0)
        new_target = [last_target, last_target2, output_lam]
        return {'last_input': last_input, 'last_target': new_target}

    def hook_mixup(self, module, input, output):
        "Interupt one run to use its intermediate results with a second model call."
        if self.intermediate_output1 is None:
            # stores intermediate output 1
            self.intermediate_output1 = output
            # restarts model with different input (using intermediate output 1 in the mixup and extracting intermediate output 2)
            self.output = self.learn.model(self.input)
            # performs mixup with intermediate output 2
            lam = _adapt_dim(self.lam, output)
            new_output = output * (1 - lam) + self.intermediate_output2 * lam
        elif self.intermediate_output2 is None:
            # stores intermediate ouput 2
            self.intermediate_output2 = output
            # performs mixup with intermediate output 1
            lam = _adapt_dim(self.lam, output)
            new_output = output * (1 - lam) + self.intermediate_output1 * lam
        elif not self._warning_raised:
            warnings.warn('One of the mixup modules defined in the model is used more than once in forward pass. Mixup will happen only at first call.', Warning)
            self._warning_raised = True
        return new_output

    def on_loss_begin(self, last_output, train, **kwargs):
        "Removes hook and stacks outputs if needed"
        if (not train) or self.is_input_mixup: return
        self.mixup_hook.remove()
        self.intermediate_output1 = None
        self.intermediate_output2 = None
        if self.use_symmetric_batch:
            last_output = torch.cat((last_output, self.output), dim=0)
            return {'last_output': last_output}

    def on_train_end(self, **kwargs):
        "Restores the original loss function"
        self.learn.loss_func = self.learn.loss_func.get_old()

class ManifoldMixupLoss(Module):
    "Adapts the loss function `criterion` to be used with manifold mixup."
    def __init__(self, criterion, reduction='mean'):
        super().__init__()
        if hasattr(criterion, 'reduction'):
            self.criterion = criterion
            self.old_red = criterion.reduction
            setattr(self.criterion, 'reduction', 'none')
        else:
            self.criterion = partial(criterion, reduction='none')
            self.old_crit = criterion
        self.reduction = reduction

    def forward(self, output, *target):
        if len(target) != 3:
            finalLoss = self.criterion(output, target[0])
        else:
            (target1, target2, lam) = target
            loss1 = self.criterion(output,target1)
            loss2 = self.criterion(output,target2)
            lam = _adapt_dim(lam, loss1)
            finalLoss = loss1 * (1-lam) + loss2 * lam
        if self.reduction == 'mean':  return finalLoss.mean()
        if self.reduction == 'sum':   return finalLoss.sum()
        return finalLoss

    def get_old(self):
        "Returns the original loss function"
        if hasattr(self, 'old_crit'):
            return self.old_crit
        elif hasattr(self, 'old_red'):
            setattr(self.criterion, 'reduction', self.old_red)
            return self.criterion

def manifold_mixup(learn:Learner, alpha:float=0.4, use_symmetric_batch:bool=True, use_input_mixup:bool=True, use_only_mixup_modules:bool=False) -> Learner:
    "Adds manifold-mixup http://proceedings.mlr.press/v97/verma19a/verma19a.pdf to `learn`."
    learn.callback_fns.append(partial(ManifoldMixupCallback, alpha=alpha, use_symmetric_batch=use_symmetric_batch, use_input_mixup=use_input_mixup, use_only_mixup_modules=use_only_mixup_modules))
    return learn

# adds manifold_mixup to Learner's available methods
Learner.manifold_mixup = manifold_mixup
