import sys
import pickle
from distutils.core import Command


class extract_dist(Command):
    """Custom distutils command to extract metadata form setup function."""
    description = "Assigns self.distribution to class attribute to make it accessible from outside a class."
    user_options = []
    class_dist = None

    def __init__(self, *args, **kwargs):
        """All the metadata attributes, that were not found are 
        set to default empty values.
        """
        Command.__init__(self, *args, **kwargs)

        for attr in ['setup_requires', 'tests_require', 'install_requires', 'conflicts',
                     'packages', 'py_modules']:
            setattr(self.distribution, attr, to_list(getattr(self.distribution, attr, [])))

        for attr in ['url', 'long_description', 'description', 'license']:
            setattr(self.distribution, attr, to_str(
                getattr(self.distribution.metadata, attr, None)))

        self.distribution.classifiers = to_list(getattr(
            self.distribution.metadata, 'classifiers', []))

        if self.distribution.entry_points and not isinstance(self.distribution.entry_points, dict):
            self.distribution.entry_points = None

    def initialize_options(self):
        """Abstract method of Command class have to be overridden."""
        pass

    def finalize_options(self):
        """Abstract method of Command class have to be overridden."""
        pass

    def run(self):
        """Assignment of distribution attribute to class_dist."""
        extract_dist.class_dist = self.distribution

    def run_subprocess(self):
        data = pickle.dumps(self.distribution)
        sys.stdout.buffer.write(data)

def to_list(var):
    """Checks if given value is a list, tries to convert, if it is not."""
    if var is None:
        return []
    if isinstance(var, str):
        var = var.split('\n')
    elif not isinstance(var, list):
        try:
            var = list(var)
        except TypeError:
            raise ValueError("{} cannot be converted to the list.".format(var))
    return var


def to_str(var):
    """Similar to to_list function, but for string attributes."""
    if not isinstance(var, str):
        return 'TODO'
    return var
