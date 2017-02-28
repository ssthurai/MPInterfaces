from __future__ import print_function, division, unicode_literals

import os

import yaml

from pymatgen.core.composition import Composition
from pymatgen.core.periodic_table import Element
from pymatgen.io.vasp.inputs import Kpoints, Incar
from pymatgen.io.vasp.outputs import Vasprun
from pymatgen.matproj.rest import MPRester

from monty.serialization import loadfn

from mpinterfaces import MY_CONFIG

from twod_materials import MPR, VASP, VASP_2D, POTENTIAL_PATH, USR, VDW_KERNEL,\
    QUEUE
import twod_materials
from twod_materials.stability.startup import relax
import twod_materials.utils as utl


PACKAGE_PATH = twod_materials.__file__.replace('__init__.pyc', '')
PACKAGE_PATH = PACKAGE_PATH.replace('__init__.py', '')

REFERENCE_MPIDS = loadfn(os.path.join(
    PACKAGE_PATH, 'pourbaix/reference_mpids.yaml')
)
EXPERIMENTAL_OXIDE_FORMATION_ENERGIES = loadfn(os.path.join(
    PACKAGE_PATH, 'pourbaix/experimental_oxide_formation_energies.yaml')
)
GAS_CORRECTIONS = loadfn(os.path.join(
    PACKAGE_PATH, 'pourbaix/gas_corrections.yaml')
)


def get_experimental_formation_energies():
    """
    Read in the raw enthalpy and entropy energy data from
    Kubaschewski in experimental_oxide_formation_energies.yaml
    and interpret it into actual formation energies. This extra
    step is written out mostly just to make the methodology clear
    and reproducible.
    """
    data = EXPERIMENTAL_OXIDE_FORMATION_ENERGIES
    oxygen_entropy = 38.48  # Entropy in atomic O, in cal/mol.degree
    formation_energies = {}
    for compound in data:
        composition = Composition(compound)
        element = [e for e in composition if e.symbol != 'O'][0]

        delta_H = data[compound]['delta_H']
        delta_S = (
            data[compound]['S_cmpd']
            - (data[compound]['S_elt']*composition[element]
               + oxygen_entropy*composition['O'])
        ) * 298 / 1000
        # Convert kcal/mole to eV/formula unit
        formation_energies[element.symbol] = (delta_H - delta_S) / 22.06035

    return formation_energies


def relax_references(potcar_types, incar_dict, submit=True):
    """
    Set up calculation directories to calibrate
    the ion corrections to match a specified framework of INCAR
    parameters and potcar hashes.

    Args:
        potcar_types (list): list of all elements to calibrate,
            containing specifications for the kind of potential
            desired for each element, e.g. ['Na_pv', 'O_s']. If
            oxygen is not explicitly included in the list, 'O_s'
            is used.

        incar_dict (dict): a dictionary of input parameters
            and their values, e.g. {'ISMEAR': -5, 'NSW': 10}

        submit (bool): whether or not to submit each job
            after preparing it.
    """

    for element in potcar_types:
        if element.split('_')[0] == 'O':
            oxygen_potcar = element
            break
    else:
        oxygen_potcar = 'O_s'
        potcar_types.append('O_s')

    for element in potcar_types:
        elt = element.split('_')[0]
        # First, set up a relaxation for the pure element.
        if not os.path.isdir(elt):
            os.mkdir(elt)
        os.chdir(elt)
        s = MPR.get_structure_by_material_id(REFERENCE_MPIDS[elt]['element'])
        s.to('POSCAR', 'POSCAR')
        relax(dim=3, incar_dict=incar_dict, submit=submit)
        utl.write_potcar(types=[element])

        # Then set up a relaxation for its reference oxide.
        if elt not in ['O', 'S', 'F', 'Cl', 'Br', 'I']:
            if not os.path.isdir('oxide'):
                os.mkdir('oxide')
            os.chdir('oxide')
            s = MPR.get_structure_by_material_id(REFERENCE_MPIDS[elt]['oxide'])
            s.to('POSCAR', 'POSCAR')
            relax(dim=3, incar_dict=incar_dict, submit=submit)
            utl.write_potcar(types=[element, oxygen_potcar])

            os.chdir('../')
        os.chdir('../')


def get_corrections(write_yaml=False):
    """
    Calculates and collects the corrections to be added for
    each reference element directory in the current working
    directory.

    Args:
        write_yaml (bool): whether or not to write the
            corrections to ion_corrections.yaml and the mu0
            values to end_members.yaml.

    Returns:
        dict. elements as keys and their corrections as values,
            in eV per atom, e.g. {'Mo': 0.135, 'S': -0.664}.
    """

    experimental_formation_energies = get_experimental_formation_energies()
    mu0, corrections = {}, {}
    special_cases = ['O', 'S', 'F', 'Cl', 'Br', 'I']

    elts = [elt for elt in os.listdir(os.getcwd()) if os.path.isdir(elt)
            and elt not in special_cases]
    special_elts = [elt for elt in os.listdir(os.getcwd()) if os.path.isdir(elt)
            and elt in special_cases]

    # Add entropic correction for special elements (S * 298K)
    for elt in special_elts:
        os.chdir(elt)
        vasprun = Vasprun('vasprun.xml')
        composition = vasprun.final_structure.composition
        formula_and_factor = composition.get_integer_formula_and_factor()
        n_formula_units = composition.get_integer_formula_and_factor()[1]
        if '2' in formula_and_factor[0]:
            n_formula_units *= 2
        
        mu0[elt] = (
            round(vasprun.final_energy / n_formula_units
                  + GAS_CORRECTIONS[elt], 3)
        )
        os.chdir('../')

    # Oxide correction from L. Wang, T. Maxisch, and G. Ceder,
    # Phys. Rev. B 73, 195107 (2006). This correction is to get
    # solid oxide formation energies right, and is for GGA+U
    mu0['O'] += 0.708

    for elt in elts:
        EF_exp = experimental_formation_energies[elt]

        os.chdir(elt)
        try:
            vasprun = Vasprun('vasprun.xml')
            composition = vasprun.final_structure.composition
            mu0[elt] = round(
                vasprun.final_energy / composition[Element(elt)], 3
            )

            # Nitrogen needs an entropic gas phase correction too.
            if elt == 'N':
                mu0[elt] -= GAS_CORRECTIONS['N']

        except Exception as e:
            corrections[elt] = 'Element not finished'

        os.chdir('oxide')
        try:
            vasprun = Vasprun('vasprun.xml')
            composition = vasprun.final_structure.composition
            n_formula_units = composition.get_integer_formula_and_factor()[1]

            EF_dft = (
                vasprun.final_energy
                - mu0[elt]*composition[Element(elt)]
                - mu0['O']*composition[Element('O')]
            ) / n_formula_units

            corrections[elt] = round(
                (EF_dft - EF_exp) / composition[Element(elt)], 3
            ) / (composition[Element(elt)] * n_formula_units)

        except UnboundLocalError:
            # The relaxation didn't finish.
            if elt in corrections:
                corrections[elt] += 'and oxide not finished'
            else:
                corrections[elt] = 'Oxide not finished'

        os.chdir('../../')

    if write_yaml:
        with open('ion_corrections.yaml', 'w') as yam:
            yam.write(yaml.dump(corrections, default_flow_style=False))
        with open('end_members.yaml', 'w') as yam:
            yam.write(yaml.dump(mu0, default_flow_style=False))

    return corrections
