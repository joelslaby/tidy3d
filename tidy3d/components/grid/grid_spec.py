""" Defines classes specifying meshing in 1D and a collective class for 3D """

from abc import ABC, abstractmethod
from typing import Tuple, List, Union

import numpy as np
import pydantic as pd

from .grid import Coords1D, Coords, Grid
from .mesher import GradedMesher, MesherType
from ..base import Tidy3dBaseModel
from ..types import Axis, Symmetry
from ..source import SourceType
from ..structure import Structure
from ...log import SetupError, log
from ...constants import C_0, MICROMETER


class GridSpec1d(Tidy3dBaseModel, ABC):

    """Abstract base class, defines 1D grid generation specifications."""

    def make_coords(  # pylint:disable = too-many-arguments
        self,
        center: float,
        size: float,
        axis: Axis,
        structures: List[Structure],
        symmetry: Symmetry,
        wavelength: pd.PositiveFloat,
        num_pml_layers: Tuple[pd.NonNegativeInt, pd.NonNegativeInt],
    ) -> Coords1D:
        """Generate 1D coords to be used as grid boundaries, based on simulation parameters.
        Symmetry, and PML layers will be treated here.

        Parameters
        ----------
        center : float
            Center of simulation domain along a given axis.
        size : float
            Size of simulation domain along a given axis.
        axis : Axis
            Axis of this direction.
        structures : List[Structure]
            List of structures present in simulation.
        symmetry : Symmetry
            Reflection symmetry across a plane bisecting the simulation domain normal
            to a given axis.
        wavelength : float
            Free-space wavelength.
        num_pml_layers : Tuple[int, int]
            number of layers in the absorber + and - direction along one dimension.

        Returns
        -------
        :class:`.Coords1D`:
            1D coords to be used as grid boundaries.
        """

        # Determine if one should apply periodic boundary condition.
        # This should only affect auto nonuniform mesh generation for now.
        is_periodic = sum(num_pml_layers) == 0

        # generate boundaries
        bound_coords = self._make_coords_initial(
            center, size, axis, structures, wavelength, is_periodic
        )

        # incooperate symmetries
        if symmetry != 0:
            # Offset to center if symmetry present
            center_ind = np.argmin(np.abs(center - bound_coords))
            bound_coords += center - bound_coords[center_ind]
            bound_coords = bound_coords[bound_coords >= center]
            bound_coords = np.append(2 * center - bound_coords[:0:-1], bound_coords)

        # Add PML layers in using dl on edges
        bound_coords = self._add_pml_to_bounds(num_pml_layers, bound_coords)
        return bound_coords

    @abstractmethod
    def _make_coords_initial(
        self,
        center: float,
        size: pd.NonNegativeFloat,
        *args,
    ) -> Coords1D:
        """Generate 1D coords to be used as grid boundaries, based on simulation parameters.
        Symmetry, PML etc. are not considered in this method.

        For auto nonuniform generation, it will take some more arguments.

        Parameters
        ----------
        center : float
            Center of simulation domain along a given axis.
        size : float
            Sie of simulation domain along a given axis.
        *args
            Other arguments

        Returns
        -------
        :class:`.Coords1D`:
            1D coords to be used as grid boundaries.
        """

    @staticmethod
    def _add_pml_to_bounds(num_layers: Tuple[int, int], bounds: Coords1D) -> Coords1D:
        """Append absorber layers to the beginning and end of the simulation bounds
        along one dimension.

        Parameters
        ----------
        num_layers : Tuple[int, int]
            number of layers in the absorber + and - direction along one dimension.
        bound_coords : np.ndarray
            coordinates specifying boundaries between cells along one dimension.

        Returns
        -------
        np.ndarray
            New bound coordinates along dimension taking abosrber into account.
        """
        if bounds.size < 2:
            return bounds

        first_step = bounds[1] - bounds[0]
        last_step = bounds[-1] - bounds[-2]
        add_left = bounds[0] - first_step * np.arange(num_layers[0], 0, -1)
        add_right = bounds[-1] + last_step * np.arange(1, num_layers[1] + 1)
        new_bounds = np.concatenate((add_left, bounds, add_right))

        return new_bounds


class UniformGrid(GridSpec1d):

    """Uniform 1D grid.

    Example
    -------
    >>> grid_1d = UniformGrid(dl=0.1)
    """

    dl: pd.PositiveFloat = pd.Field(
        ...,
        title="Grid Size",
        description="Grid size for uniform grid generation.",
        units=MICROMETER,
    )

    def _make_coords_initial(
        self,
        center: float,
        size: float,
        *args,
    ) -> Coords1D:
        """Uniform 1D coords to be used as grid boundaries.

        Parameters
        ----------
        center : float
            Center of simulation domain along a given axis.
        size : float
            Size of simulation domain along a given axis.
        *args:
            Other arguments all go here.

        Returns
        -------
        :class:`.Coords1D`:
            1D coords to be used as grid boundaries.
        """

        # Take a number of steps commensurate with the size; make dl a bit smaller if needed
        num_cells = int(np.ceil(size / self.dl))

        # Make sure there's at least one cell
        num_cells = max(num_cells, 1)

        # Adjust step size to fit simulation size exactly
        dl_snapped = size / num_cells if size > 0 else self.dl

        # Make bounds
        bound_coords = center - size / 2 + np.arange(num_cells + 1) * dl_snapped

        return bound_coords


class CustomGrid(GridSpec1d):

    """Custom 1D grid supplied as a list of grid cell sizes centered on the simulation center.

    Example
    -------
    >>> grid_1d = CustomGrid(dl=[0.2, 0.2, 0.1, 0.1, 0.1, 0.2, 0.2])
    """

    dl: List[pd.PositiveFloat] = pd.Field(
        ...,
        title="Customized grid sizes.",
        description="An array of custom nonuniform grid sizes. The resulting grid is centered on "
        "the simulation center such that it spans the region "
        "``(center - sum(dl)/2, center + sum(dl)/2)``. "
        "Note: if supplied sizes do not cover the simulation size, the first and last sizes "
        "are repeated to cover the simulation domain.",
        units=MICROMETER,
    )

    def _make_coords_initial(
        self,
        center: float,
        size: float,
        *args,
    ) -> Coords1D:
        """Customized 1D coords to be used as grid boundaries.

        Parameters
        ----------
        center : float
            Center of simulation domain along a given axis.
        size : float
            Size of simulation domain along a given axis.
        *args
            Other arguments all go here.

        Returns
        -------
        :class:`.Coords1D`:
            1D coords to be used as grid boundaries.
        """

        # get bounding coordinates
        dl = np.array(self.dl)
        bound_coords = np.append(0.0, np.cumsum(dl))

        # place the middle of the bounds at the center of the simulation along dimension
        bound_coords += center - bound_coords[-1] / 2

        # chop off any coords outside of simulation bounds
        bound_min = center - size / 2
        bound_max = center + size / 2
        bound_coords = bound_coords[bound_coords <= bound_max]
        bound_coords = bound_coords[bound_coords >= bound_min]

        # if not extending to simulation bounds, repeat beginning and end
        dl_min = dl[0]
        dl_max = dl[-1]
        while bound_coords[0] - dl_min >= bound_min:
            bound_coords = np.insert(bound_coords, 0, bound_coords[0] - dl_min)
        while bound_coords[-1] + dl_max <= bound_max:
            bound_coords = np.append(bound_coords, bound_coords[-1] + dl_max)

        return bound_coords


class AutoGrid(GridSpec1d):
    """Specification for non-uniform grid along a given dimension.

    Example
    -------
    >>> grid_1d = AutoGrid(min_steps_per_wvl=16, max_scale=1.4)
    """

    min_steps_per_wvl: float = pd.Field(
        10.0,
        title="Minimal number of steps per wavelength",
        description="Minimal number of steps per wavelength in each medium.",
        ge=6.0,
    )

    max_scale: float = pd.Field(
        1.4,
        title="Maximum Grid Size Scaling",
        description="Sets the maximum ratio between any two consecutive grid steps.",
        ge=1.2,
        lt=2.0,
    )

    mesher: MesherType = pd.Field(
        GradedMesher(),
        title="Grid Construction Tool",
        description="The type of mesher to use to generate the grid automatically.",
    )

    def _make_coords_initial(  # pylint:disable = arguments-differ, too-many-arguments
        self,
        center: float,
        size: float,
        axis: Axis,
        structures: List[Structure],
        wavelength: float,
        is_periodic: bool,
    ) -> Coords1D:
        """Customized 1D coords to be used as grid boundaries.

        Parameters
        ----------
        center : float
            Center of simulation domain along a given axis.
        size : float
            Size of simulation domain along a given axis.
        axis : Axis
            Axis of this direction.
        structures : List[Structure]
            List of structures present in simulation.
        wavelength : float
            Free-space wavelength.
        is_periodic : bool
            Apply periodic boundary condition or not.

        Returns
        -------
        :class:`.Coords1D`:
            1D coords to be used as grid boundaries.
        """

        # parse structures
        interval_coords, max_dl_list = self.mesher.parse_structures(
            axis, structures, wavelength, self.min_steps_per_wvl
        )

        # generate mesh steps
        interval_coords = np.array(interval_coords).flatten()
        max_dl_list = np.array(max_dl_list).flatten()
        len_interval_list = interval_coords[1:] - interval_coords[:-1]
        dl_list = self.mesher.make_grid_multiple_intervals(
            max_dl_list, len_interval_list, self.max_scale, is_periodic
        )

        # generate boundaries
        bound_coords = np.append(0.0, np.cumsum(np.concatenate(dl_list)))
        bound_coords += interval_coords[0]
        return np.array(bound_coords)


GridType = Union[UniformGrid, CustomGrid, AutoGrid]


class GridSpec(Tidy3dBaseModel):

    """Collective grid specification for all three dimensions.

    Example
    -------
    >>> uniform = UniformGrid(dl=0.1)
    >>> custom = CustomGrid(dl=[0.2, 0.2, 0.1, 0.1, 0.1, 0.2, 0.2])
    >>> auto = AutoGrid(min_steps_per_wvl=12)
    >>> grid_spec = GridSpec(grid_x=uniform, grid_y=custom, grid_z=auto, wavelength=1.5)
    """

    grid_x: GridType = pd.Field(
        AutoGrid(),
        title="Grid specification along x-axis",
        description="Grid specification along x-axis",
    )

    grid_y: GridType = pd.Field(
        AutoGrid(),
        title="Grid specification along y-axis",
        description="Grid specification along y-axis",
    )

    grid_z: GridType = pd.Field(
        AutoGrid(),
        title="Grid specification along z-axis",
        description="Grid specification along z-axis",
    )

    wavelength: float = pd.Field(
        None,
        title="Free-space wavelength",
        description="Free-space wavelength for automatic nonuniform grid. It can be 'None' "
        "if there is at least one source in the simulation, in which case it is defined by "
        "the source central frequency.",
        units=MICROMETER,
    )

    @property
    def auto_grid_used(self) -> bool:
        """True if any of the three dimensions uses :class:`.AutoGrid`."""
        grid_list = [self.grid_x, self.grid_y, self.grid_z]
        return np.any([isinstance(mesh, AutoGrid) for mesh in grid_list])

    def get_wavelength(self, sources: List[SourceType]) -> pd.PositiveFloat:
        """Define a wavelength based on attribute or supplied sources."""

        if self.wavelength is not None or not self.auto_grid_used:
            return self.wavelength

        # If auto mesh used and wavelength is None, use central frequency of sources, if any.
        freqs = np.array([source.source_time.freq0 for source in sources])
        # no sources
        if len(freqs) == 0:
            raise SetupError(
                "Automatic grid generation requires the input of 'wavelength' or sources."
            )
        # multiple sources of different central frequencies
        if len(freqs) > 0 and not np.all(np.isclose(freqs, freqs[0])):
            raise SetupError(
                "Sources of different central frequencies are supplied. "
                "Please supply a 'wavelength' value for 'grid_spec'."
            )

        wavelength = C_0 / freqs[0]
        log.info(f"Auto meshing using wavelength {wavelength:1.4f} defined from sources.")
        return wavelength

    def make_grid(
        self,
        structures: List[Structure],
        symmetry: Tuple[Symmetry, Symmetry, Symmetry],
        sources: List[SourceType],
        num_pml_layers: List[Tuple[pd.NonNegativeInt, pd.NonNegativeInt]],
    ) -> Grid:
        """Make the entire simulation grid based on some simulation parameters.

        Parameters
        ----------
        structures : List[Structure]
            List of structures present in the simulation. The first structure must be the
            simulation geometry with the simulation background medium.
        symmetry : Tuple[Symmetry, Symmetry, Symmetry]
            Reflection symmetry across a plane bisecting the simulation domain
            normal to each of the three axes.
        sources : List[SourceType]
            List of sources.
        num_pml_layers : List[Tuple[float, float]]
            List containing the number of absorber layers in - and + boundaries.

        Returns
        -------
        Grid:
            Entire simulation grid.
        """

        center, size = structures[0].geometry.center, structures[0].geometry.size

        # Set up wavelength for automatic mesh generation if needed.
        wavelength = self.get_wavelength(sources)

        coords_x = self.grid_x.make_coords(
            center=center[0],
            size=size[0],
            axis=0,
            structures=structures,
            symmetry=symmetry[0],
            wavelength=wavelength,
            num_pml_layers=num_pml_layers[0],
        )
        coords_y = self.grid_y.make_coords(
            center=center[1],
            size=size[1],
            axis=1,
            structures=structures,
            symmetry=symmetry[1],
            wavelength=wavelength,
            num_pml_layers=num_pml_layers[1],
        )
        coords_z = self.grid_z.make_coords(
            center=center[2],
            size=size[2],
            axis=2,
            structures=structures,
            symmetry=symmetry[2],
            wavelength=wavelength,
            num_pml_layers=num_pml_layers[2],
        )

        coords = Coords(x=coords_x, y=coords_y, z=coords_z)
        return Grid(boundaries=coords)

    @classmethod
    def auto(
        cls,
        wavelength: pd.PositiveFloat = None,
        min_steps_per_wvl: pd.PositiveFloat = 10.0,
        max_scale: pd.PositiveFloat = 1.4,
        mesher: MesherType = GradedMesher(),
    ) -> "GridSpec":
        """Use the same :class:`AutoGrid` along each of the three directions.

        Parameters
        ----------
        wavelength : float = None
            Free-space wavelength for automatic nonuniform grid. It can be 'None'
            if there is at least one source in the simulation, in which case it is defined by
            the source central frequency.
        min_steps_per_wvl : float, optional
            Minimal number of steps per wavelength in each medium.
        max_scale : float, optional
            Sets the maximum ratio between any two consecutive grid steps.

        Returns
        -------
        GridSpec
            :class:`GridSpec` with the same automatic nonuniform grid settings in each direction.
        """

        grid_1d = AutoGrid(min_steps_per_wvl=min_steps_per_wvl, max_scale=max_scale, mesher=mesher)
        return cls(wavelength=wavelength, grid_x=grid_1d, grid_y=grid_1d, grid_z=grid_1d)

    @classmethod
    def uniform(cls, dl: float) -> "GridSpec":
        """Use the same :class:`UniformGrid` along each of the three directions.

        Parameters
        ----------
        dl : float
            Grid size for uniform grid generation.

        Returns
        -------
        GridSpec
            :class:`GridSpec` with the same uniform grid size in each direction.
        """

        grid_1d = UniformGrid(dl=dl)
        return cls(grid_x=grid_1d, grid_y=grid_1d, grid_z=grid_1d)