from abc import ABC

class SingletonMeta(type):
  _instances = {}

  def __call__(cls, *args, **kwargs):
    if cls not in cls._instances:
      cls._instances[cls] = super().__call__(*args, **kwargs)
    return cls._instances[cls]
  

# metaclass for singleton with ABC
class SingletonABCMeta(type(ABC),type):
  """
  The Singleton class can be implemented in different ways in Python. Some
  possible methods include: base class, decorator, metaclass. We will use the
  metaclass because it is best suited for this purpose.
  """

  _instances = {}

  def __call__(cls, *args, **kwargs):
    if cls not in cls._instances:
      cls._instances[cls] = super().__call__(*args, **kwargs)
    return cls._instances[cls]

#NOTE: we suppose each model have different name for all ai companies.
# for each ai company, we have only one class for it.
# metaclass for singleton
class SingleTonMetaOfModel(type(ABC),type):
  """
  The Singleton class can be implemented in different ways in Python. Some
  possible methods include: base class, decorator, metaclass. We will use the
  metaclass because it is best suited for this purpose.
  """

  _instances = {}

  def __call__(cls, model, *args, **kwargs):
    """
    Possible changes to the value of the `__init__` argument do not affect
    the returned instance.
    """
    key = (cls,model)
    if key not in cls._instances:
      instance = super().__call__(model, *args, **kwargs)
      cls._instances[key] = instance
    return cls._instances[key]
