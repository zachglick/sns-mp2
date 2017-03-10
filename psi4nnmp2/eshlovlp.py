from psi4 import core
import numpy as np


class ESHLOVLPDecomposition(object):
    #def __init__(self, m1_basis, m2_basis, dimer_basis, dimer_aux_basis):
    def __init__(self, m1_basis, m2_basis, dimer_basis, dimer_jk):
        self.dimer_basis = dimer_basis
        #self.dimer_aux_basis = dimer_aux_basis

        self.dimer_V, self.dimer_T, self.dimer_S = self._initialize_mints(dimer_basis)
        self.monomer1_V = self._initialize_mints(m1_basis, v_only=True)
        self.monomer2_V = self._initialize_mints(m2_basis, v_only=True)
        self.jk = dimer_jk

        self.nuclear_interaction_energy = (
            dimer_basis.molecule().nuclear_repulsion_energy() -
            m1_basis.molecule().nuclear_repulsion_energy() -
            m2_basis.molecule().nuclear_repulsion_energy())


    # def _initialize_jk(self, basis, aux_basis, do_J=True, do_K=True):
    #     jk = core.JK.build(basis, aux_basis)
    #     jk.set_memory(int(float(core.get_global_option("SCF_MEM_SAFETY_FACTOR")) * core.get_memory()) / 8)
    #     jk.set_do_J(do_J)
    #     jk.set_do_K(do_K)
    #     jk.print_header()
    #     jk.initialize()
    #     return jk

    def _initialize_mints(self, basis, v_only=False):
        mints = core.MintsHelper(basis)
        V = mints.ao_potential()
        if v_only:
            return V

        T = mints.ao_kinetic()
        S = mints.ao_overlap()
        return V, T, S

    def hl(self):
        return HeitlerLondonFunctor(self)

    def esovlp(self):
        return EletrostaticsOverlapFunctor(self)


class HeitlerLondonFunctor(object):
    def __init__(self, energy_decomposition):
        self.p = energy_decomposition
        #self.jk = self.p._initialize_jk(self.p.dimer_basis, self.p.dimer_aux_basis)
        self.jk = self.p.jk

    #def __del__(self):
    #    self.jk.finalize()

    def __call__(self, mol1_wfn, mol2_wfn):
        nbf = self.p.dimer_basis.nbf()
        nocc = mol1_wfn.nalpha() + mol2_wfn.nalpha()

        # Take the occupied orbitals from the two HF monomer wavefunctions
        # and pack them (block diagonal) into the dimer basis set.
        m1_OCC = mol1_wfn.Ca_subset('SO', 'OCC')
        m2_OCC = mol2_wfn.Ca_subset('SO', 'OCC')

        C = core.Matrix(nbf, nocc)
        C.np[:mol1_wfn.nso(), :mol1_wfn.nalpha()] = m1_OCC.np[:, :]
        C.np[-mol2_wfn.nso():, -mol2_wfn.nalpha():] = m2_OCC.np[:, :]

        C = orthogonalize(C, self.p.dimer_S)

        # At this point, it should be the case that
        # C.T * S * C == I
        np.testing.assert_array_almost_equal(
            core.Matrix.triplet(C, self.p.dimer_S, C, True, False, False),
            np.eye(nocc))

        self.jk.C_clear()
        self.jk.C_left_add(C)
        self.jk.compute()

        J = self.jk.J()[0]
        K = self.jk.K()[0]
        D = self.jk.D()[0]

        # 2T + 2V + 2J - K

        FH = J.clone()
        FH.zero()
        FH.axpy(2, self.p.dimer_T)
        FH.axpy(2, self.p.dimer_V)
        FH.axpy(2, J)
        FH.axpy(-1, K)

        energy = FH.vector_dot(D) + self.p.dimer_basis.molecule().nuclear_repulsion_energy()
        hl = energy - (mol1_wfn.energy() + mol2_wfn.energy())
        return hl


class EletrostaticsOverlapFunctor(object):
    def __init__(self, energy_decomposition):
        self.p = energy_decomposition
        #self.jk = self.p._initialize_jk(self.p.dimer_basis, self.p.dimer_aux_basis, do_J=True, do_K=False)
        self.jk = self.p.jk

    # def __del__(self):
    #     self.jk.finalize()

    def __call__(self, mol1_wfn, mol2_wfn):
        nbf = self.p.dimer_basis.nbf()
        nbf1 = mol1_wfn.nso()
        nbf2 = mol2_wfn.nso()

        U, S, VT = np.linalg.svd(mol1_wfn.Da())
        C_left = np.dot(U, np.diag(np.sqrt(S)))
        C_right = np.dot(VT.T, np.diag(np.sqrt(S)))

        C_left_ = core.Matrix(nbf, max(nbf1, nbf2))
        C_right_ = core.Matrix(nbf, max(nbf1, nbf2))

        C_left_.np[:nbf1, :] = C_left[:, :]
        C_right_.np[:nbf1, :] = C_right[:, :]

        self.jk.C_clear()
        self.jk.C_left_add(C_left_)
        self.jk.C_right_add(C_right_)
        self.jk.compute()

        J = self.jk.J()[0]
        D1 = self.jk.D()[0]
        print(np.max(np.abs(D1.np[:nbf1, :nbf1] - mol1_wfn.Da().np)))
        J_1to2 = J.np[nbf1:, nbf1:]
        elel_1to2 = 2 * np.sum(J_1to2 * mol2_wfn.Da())
        nuel_1to2 = 2 * (self.p.dimer_V.vector_dot(D1) - self.p.monomer1_V.vector_dot(mol1_wfn.Da()))
        ovlp1 = core.Matrix.doublet(self.p.dimer_S, D1, False, False)

        #######################################################################

        U, S, VT = np.linalg.svd(mol2_wfn.Da())
        C_left = np.dot(U, np.diag(np.sqrt(S)))
        C_right = np.dot(VT.T, np.diag(np.sqrt(S)))

        C_left_ = core.Matrix(nbf, max(nbf1, nbf2))
        C_right_ = core.Matrix(nbf, max(nbf1, nbf2))

        C_left_.np[-nbf2:, :] = C_left[:, :]
        C_right_.np[-nbf2:, :] = C_right[:, :]

        self.jk.C_clear()
        self.jk.C_left_add(C_left_)
        self.jk.C_right_add(C_right_)
        self.jk.compute()

        J = self.jk.J()[0]
        D2 = self.jk.D()[0]
        print(np.max(np.abs(D2.np[nbf1:, nbf1:] - mol2_wfn.Da().np)))

        J_2to1 = J.np[:nbf1, :nbf1]
        elel_2to1 = 2 * np.sum(J_2to1 * mol1_wfn.Da())
        nuel_2to1 = 2 * (self.p.dimer_V.vector_dot(D2) - self.p.monomer2_V.vector_dot(mol2_wfn.Da()))

        ovlp2 = core.Matrix.doublet(self.p.dimer_S, D2, False, False)

        overlap = 4 * np.sum(ovlp1.np * ovlp2.np.T)
        #assert abs(elel_1to2 - elel_2to1) < 1e-10
        print('ELEL', elel_1to2, elel_2to1)

        electrostatic = self.p.nuclear_interaction_energy + nuel_1to2 + nuel_2to1 + elel_1to2 + elel_2to1
        print(mol1_wfn.name())
        print("electrostatic", electrostatic)
        print('overlap', overlap)
        if mol2_wfn.name() == 'DF-MP2':
            import IPython; IPython.embed()
        return electrostatic, overlap

        # NUEL_MCBS_MP_0 = tr[DEN*EPOT] = -268.06696509          -268.0670121846953              (0.0296)
        # NUEL_MCBS_MP_1 = tr[DEN*EPOT] = -268.06696453          -268.06701170727615             (0.0296)
        # ELELMP01 = 2 tr[DENS_MP_1*COUL_MP_0] = 44.34275352     44.342843974721859              (-0.057)
        # ELELMP10 = 2 tr[DENS_MP_0*COUL_MP_1] = 44.34275352     44.342843974617708              (-0.057)
        # NUEL_MP_1 = tr[DENS_MP_1*EPOT] = -312.418482           -312.4187450292606              (0.165)
        # NUEL_MP_0 = tr[DENS_MP_0*EPOT] = -312.418482           -312.4187450292606              (0.165)
        # NUNUINT                          44.35906177           44.359061738529945              (0.00001975)
        # -------------------------------------------------------------------------------------------------
        #                         -0.0012190899999922067        -0.0015604534021207428           (0.214)
        # nunu_int + nuel_mp_0 - nuel_mcbs_mp_0 + nuel_mp_1 - nuel_mcbs_mp_1 + elelmp01


def orthogonalize(C, S):
    nbf, nocc = C.shape

    eigenvectors = core.Matrix(nocc, nocc)
    eigvals = core.Vector(nocc)
    sqrt_eigvals = core.Vector(nocc)

    CTSC = core.Matrix.triplet(C, S, C, True, False, False)
    CTSC.diagonalize(eigenvectors, eigvals, core.DiagonalizeOrder.Ascending)

    orthonormal = core.Matrix.doublet(C, eigenvectors, False, False)

    sqrt_eigvals.np[:] = np.sqrt(eigvals.np)
    orthonormal.np[:, :] /= sqrt_eigvals.np[np.newaxis, :]

    return orthonormal
