# Smoke Test

- Python version: 3.12.10
- CadQuery version: 2.8.0
- Validator exit code: 1 (expected; baseline plate fails bone_conformance_gap)

## Validator output

```
Iteration 0: FAIL (19/20 checks passing)
========================================

CHECK                        STATUS      VALUE / LIMIT      UNIT 
------------------------------------------------------------------------------
manifold_watertight          PASS            1 / 1          bool 
envelope_length              PASS          180 / 180        mm   
envelope_width               PASS           16 / 20         mm   
envelope_standoff            PASS        2.998 / 6          mm   
min_wall_thickness           PASS            3 / 2.5        mm     at (34.99, -6, 102)
implant_mass                 PASS       36.996 / 39         g    
no_bone_collision            PASS            0 / 0          vertices
bone_conformance_gap         FAIL        6.166 / 1.5        mm     at (34.99, 0, 100)
    -> implant stands 6.17 mm off the bone at z=100 mm; the plate must follow the contour of the shaft
screw_trajectories_clear     PASS            6 / 6          count
keepout_perforating_vessel_bundle PASS            0 / 0          mm     at (34.93, 14.8, 190)
keepout_proximal_neurovascular_corridor PASS            0 / 0          mm     at (27.38, 0, 85)
keepout_distal_physeal_scar  PASS            0 / 0          mm     at (30.04, 0, 300)
stress_max_bending           SKIP            - / 350        MPa  
stress_hole_0                SKIP            - / 350        MPa  
stress_hole_1                SKIP            - / 350        MPa  
stress_hole_2                SKIP            - / 350        MPa  
stress_hole_3                SKIP            - / 350        MPa  
stress_hole_4                SKIP            - / 350        MPa  
stress_hole_5                SKIP            - / 350        MPa  
screw_pullout_min            SKIP            - / 1200       N    

FAILING: bone_conformance_gap
```
