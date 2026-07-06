# Reference Digest — Subsea Cable Lay Physics

Working notes distilled from the documents in `ref/`, gathered while planning
Catenary V3. Equations are transcribed in plain text; symbols follow each
source. This file is a convenience digest — always check the source paper
before relying on an equation in shipped code.

---

## 1. Zajac 1957 — *Dynamics and Kinematics of the Laying and Recovery of Submarine Cable* (BSTJ 36(5):1129–1207)

The canonical steady-lay-with-drag theory. Full paper digested below.

### Assumptions
Perfectly flexible cable (no EI), constant ship speed `V`, constant pay-out
rate `V_c`, still sea, flat bottom, drag a function of relative water
velocity only. In the ship frame the laying configuration is stationary.

### Straight-line (critical-angle) solution — normal laying
With zero bottom tension the *only* configuration satisfying the boundary
conditions is a straight line from ship to touchdown at the **critical
angle** `alpha` to the horizontal:

- Normal force balance:      `w*cos(alpha) = D_N`                      (1)
- Ship tension:              `T_s = w*L*sin(alpha) - D_T*L ≈ w*h`       (2,3)
- Normal drag (quadratic):   `D_N = C_D * rho * V_N^2 * d / 2`,
  `V_N = V*sin(alpha)`                                                  (4,5)
- Combining:                 `w*cos(alpha) = (C_D*rho*V^2*d/2) * sin^2(alpha)` (6)
- **Hydrodynamic constant**: `H = sqrt(2w / (C_D * rho * d))`            (8)
  (equals the transverse terminal sinking velocity `u_s`, eq. 16)
- Small-angle:               `alpha_0 = H / V` (radians)                 (9)
- Exact:                     `cos(alpha) = sqrt(1 + (1/4)*(H/V)^4) - (1/2)*(H/V)^2` (10)
  (small-angle form good below ~20 deg)

### Tangential drag
- Smooth (PE) cable: skin friction `D_T = C_f * (1/2)*rho*V_t^2*pi*d`,
  `C_f = 0.055 / Re^0.14`, `Re = V_t*L/nu`; length-dependent, order-of-
  magnitude only at deep-sea lengths.                                    (12,14)
- Jute-served cable: `D_T = 0.01 * V_t^1.48` (lb/ft with V_t in ft/s;
  dimensional).                                                          (15)
- Tangential relative velocity while laying: `V_t = V_c - V*cos(alpha)`  (13)
- `D_T` is typically ~6% of `w*sin(alpha)` and often neglected.

### General 2-D stationary equations (element balance, ship frame)
- `(T - rho_c*V_c^2) * dtheta/ds + (C_D*rho*V^2*d/2)*sin(theta)*|sin(theta)| - w*cos(theta) = 0`  (18a)
- `dT/ds + D_T - w*sin(theta) = 0`                                       (18b)
  (`rho_c*V_c^2` is the centrifugal term from material transport along the
  stationary configuration; `theta = alpha` solves 18a.)
- Touchdown boundary condition: at the bottom either `T = 0` (straight
  line) or `theta = 0` (laying) / `pi` (recovery).
- **Top-tension theorem** (D_T = 0, any normal-drag law, holds in 3-D):
  `T_s = T_0 + w*h`                                                      (21)

### Laying with bottom tension / negative slack
- Configuration angle vs height (Appendix C):
  `tan(theta/2) = tan(alpha/2) * [ (1 - (T0b/(T0b+yb))^gamma) / (1 + (T0b/(T0b+yb))^gamma * tan^4(alpha/2)) ]^(1/2)`
  with `yb = y/h`, `T0b = T_0/(w*h)`, `gamma = (2 - sin^2 alpha)/sin^2 alpha`. (22,23)
  Configuration is near-straight except a short arc near the bottom, even
  for `T0b` up to 3–4.
- Suspended length / layback with bottom tension (small alpha):
  `S = L + kappa*T_0/w`, `X = L*cos(alpha) + lambda*T_0/w` (kappa, lambda
  from Fig. 10; e.g. `lambda - kappa ≈ 1.4e-3` at alpha = 11.7 deg).     (24)
- Negative slack `delta` (pay-out < ship speed), no bottom slip:
  `T_0 = (w/(lambda - kappa)) * delta*V*t`, so ship tension climbs
  linearly with time — quickly signals negative slack. With cable
  extensibility `EA` the rise rate drops substantially and becomes
  depth-dependent.                                                       (25)
- Recovery: normal drag pushes the cable *down*, ship tension exceeds
  laying tension; `(Tsb - 1)/Tsb = [tan^2(alpha) * (cos alpha + cos alpha_s)/(1 - cos alpha * cos alpha_s)]^(1/gamma)`. (26)

### Sinking kinematics
Element vertical rate `V_c*sin(alpha)` (repeater sink time
`tau = h/(V_c sin alpha)`); configuration descent rate `V*tan(alpha)`
(time until the cable lands on a feature the ship passed:
`theta = h/(V tan alpha)`). Both ≈ `u_s = H` at usual speeds.

### Sloped bottom and slack control (Section V–VI)
- Descent onto a slope of angle `beta`: required pay-out
  `V_c = V*(sin alpha + sin beta)/sin(alpha + beta)`               (30)
  slack `eps = (V_c - V)/V`; small-angle fill `f = H*beta/(2V)`, i.e.
  `V_c - V = H*beta/2` — **the pay-out increment for a slope is
  independent of ship speed**.                                      (33–35)
- Ascent past a crest of angle `gamma`: `V - V_c = H*gamma/2`       (36)
- **Suspension criterion**: free spans form on an up-slope of angle
  `gamma` unless `V < H/gamma` — a direct ship-speed limit over rough
  bathymetry (cable No. 2, H = 70 deg-kn, gamma = 35 deg → V < 2 kn). (37)
- Mean tension rate on a constant slope:
  `dT/dt = w*V*sin(alpha)*sin(beta)/sin(alpha+beta)` (descent).      (38)
- Shipboard tension is *insensitive* to slack (8,400→8,020 lb going from
  0→6% slack at 2,000 fathoms) → **slack must be controlled kinematically
  (depth-fed pay-out scheduling), not by tension**.

### Transients (Section IV, Appendix D)
- Longitudinal ship motion / pay-out change: 1-D wave equation with
  characteristic impedance `sqrt(EA*rho_c)`; snap tension for a pay-out
  rate change `dP/dt`: `T_p = -sqrt(EA*rho_c) * dP/dt`               (29)
  (cable No. 2: 220 lb per ft/s twist-free, 400 restrained). Brake
  seizure at 6 kn: +2,180/3,970 lb until the wave reflects off the
  bottom (~18 s round trip at 3,000 fathoms, c1 ≈ 2,000 ft/s).
- Transverse ship motion decays within ~100–200 ft of cable (drag
  damping) — negligible for shape.
- Quasi-static stepping is justified when manoeuvre time >> `2h/c1`
  (~20 s in deep water) — Zajac himself treats slack transients as a
  sequence of stationary states.

### Recovery (Section 3.7)
Normal drag pushes the cable *down* during recovery — recovery tension is
the strength design case: `(Tsb - 1)/Tsb = [tan^2(alpha)*(cos alpha +
cos alpha_s)/(1 - cos alpha*cos alpha_s)]^(1/gamma)` with
`Tsb = T_s/(w*h)`. Example: 2,000 fathoms, 1 kn, alpha = 60 deg →
`T_s = 4.85*w*h` at alpha_s = 40 deg. Shea's method (steam toward the
cable, alpha_s > 90 deg) restores the straight line: `T_s = w*h + D_T*L`.

### 3-D stationary model with cross-currents (Section VII, App. F)
Spherical-polar element equations (47a–c) + geometry (48); the
top-tension theorem holds in 3-D: `T = T_0 + w*eta` (49). Uniform
cross-current stratum (depth h', total h, current V_w at angle beta):
resultant `V' = sqrt((V - V_w cos beta)^2 + (V_w sin beta)^2)`,
heading offset `tan(phi) = V_w sin(beta)/(V - V_w cos beta)` (50–51);
closed-form touchdown offsets d (astern) and e (lateral) via a
perturbation solution (52). Worked example: 6,000 ft depth, 6 kn ship,
1 kn current over top 600 ft at 60 deg → lateral offset e = 253 ft.
**These closed forms are the validation set for V3's 3-D drag solver.**

### Appendix E — suspended cable / bight tension rise
Cable caught (or held) at a point A while the ship steams on: solved as a
**catenary on the A-side matched to a stationary lay configuration on the
ship side** — precisely the construction for a bight or fouled-cable
scenario, with worked example (2% slack, 6 kn, 6,000 ft → T_s = 1.80*w*h
after 10 min and climbing). Use as a limiting-case check for the final
bight simulation.

### Appendix A — uniqueness of the straight line
Non-straight zero-bottom-tension configurations are bounded by
`T < rho_c*V_c^2` (~6 lb for cable No. 2 at 6 kn) and cannot reach the
surface: the straight line is the only realistic zero-`T_0` laying shape.

### Measured constants (Table I, App. B)
- Cable No. 1 (0.75 in PE, 0.243 lb/ft submerged): `C_D = 1.11` measured
  (theory ~1.0 smooth cylinder); H = 67–70 deg-kn computed, 64 measured;
  C_D varies only ~4% over 0.25–10 kn → constant-H assumption sound.
- Cable No. 2 (1.25 in jute, 0.705 lb/ft, ≈ type D transatlantic):
  `C_D = 1.55`; `EA = 4e6 lb` (twist restrained) / `1.2e6 lb` (free);
  `H = 70 degree-knots`.
- `H` is measurable at sea from the stern angle: `alpha_0 * V = H` —
  treat H as a **per-cable calibration input** in V3.

---

## 2. Jack, Leech & Lewis 1957 — *Route Selection and Cable Laying for TAT-1* (BSTJ 36(1):293–326)

Operational/case-study companion to Zajac: route compromise, survey
interpretation, slack planning and vessel practice for the first
transatlantic telephone cable (no solver math). Operational ground truth
worth encoding as defaults/heuristics:
- Slack practice: ~5% desirable in deep water, stepping down to zero in
  shallow water; cable mileage = geographic distance + slack allowance.
- Lay routine: 6–7 kn pay-out; slow to ~3 kn while a flexible repeater
  passes overboard; 5 kn during splice/equalization holds.
- "Stopping of the ship in deep water introduces serious possibility of
  formation of kinks in the cable, and is to be avoided at all costs."
- Rigid repeaters (~1,200 lb) required stopping the ship and launching by
  bow gantry — the historical template for a body-deployment operation.
- Sheaves/drums sized to the repeater/cable minimum bend radius (~3.5 ft
  for TAT-1 flexible repeaters).

---

## 3. Mamatsopoulos, Michailides & Theotokoglou 2020 — *S-Lay Installation Tool with "In and Out of Water" Segments* (JMSE 8:48)

Static 2-D catenary tool; the innovation is a **two-segment catenary**
(submerged weight `q1`, in-air weight `q2` between waterline and chute)
matched at the sea surface, plus capstan chute friction. No drag, no
dynamics, inextensible, EI = 0 (justified via stiffened-catenary boundary-
layer argument).

Key relations (H = horizontal tension, x from TDP):
- `y = (H/q)(cosh(qx/H) - 1)`; `x = (H/q) acosh(yq/H + 1)`
- `S = (H/q) sinh(qx/H)`; `V = H sinh(qx/H) = q*S`; `T = sqrt(H^2+V^2) = H + q*y`
- Bend radius `R = (1 + sinh^2(qx/H))^{3/2} / ((q/H) cosh(qx/H))`;
  minimum at TDP: `R_min = H/q`.
- Chute (capstan/Euler): `T_out = T_in * e^(mu*phi)`, `phi` = wrap angle =
  exit angle; laying vs recovery flips the sign of the exponent.
- Two-segment matching: iterate in-air catenary height until vertical
  force is continuous at the waterline (H constant throughout).

**Validation fixtures** (Prysmian 3×500 mm² cable: OD 144 mm, 37 kg/m air,
23 kg/m water, EI 10 kN·m², MBR 2.2 m; water depth 93 m):

| H (kg) | layback (m) | susp. length (m) | exit angle (deg) | min R (m) |
|---|---|---|---|---|
| 1200 | 89.03 | 139.00 | 69.71 | 52.17 |
| 2000 | 119.72 | 161.31 | 62.01 | 86.96 |
| 4000 | 175.46 | 206.80 | 50.29 | 173.91 |

(All within 6.5% of a full RSTAB FEA benchmark; FEA seabed springs for
sand: axial stiffness 100–250 kN/m², mu_axial 0.4–0.6, mu_lateral ~0.8,
vertical stiffness 200–10,000 kN/m².)

Ignoring the in-air segment barely changes layback/length (<2.7%) but errs
exit angle up to 12.5% and **MBR up to ~55% in shallow water** (the true
curvature minimum moves to the air/water interface).

---

## 4. Mamatsopoulos et al. 2020 — *Critical Water Depth and Installation Curves* (JMSE 8:838)

Extension of the above. Highlights:
- `MBR_submerged = H/q1` (depth-independent); in-air minimum radius at the
  waterline grows with depth.
- **Critical Water Depth** below which the in-air segment must be modelled
  (quartic fit, H in tf, CWD in m):
  `CWD = 0.0698 H^4 - 1.2321 H^3 + 6.5376 H^2 + 2.9029 H + 3.1488`
  (anchors: 0.5 tf → 6 m … 8 tf → 100 m). MBR error if ignored: 35–59%.
- Tension budget: `T_tensioner = (q1*WD + q2*c) + H ± T_dyn ± F_r`
  (T_dyn named but explicitly not formulated — calm-water quasi-static).
- Friction sensitivity case: assuming chute mu = 0.5 when actual is 0
  collapses bottom tension 3000 → 130 kgf (TDP compression/buckling risk);
  the reverse gives 3× overtension. mu in 0.25–0.5 acts as a natural
  tension compensator. Make chute mu an explicit input with sensitivity.

---

## 5. Ohta & Nishiyama 2010 — NEC Technical Journal 5(1) (`100111.pdf`)

Operational workflow only (no equations): survey → RPL (route position
list: per-event lat/lon, cable type, equipment, lay method, alter-course
points) → SLD → loading → lay → burial. Constraints worth encoding:
stern sheave/chute diameter vs cable MBR; burial to ~1,000 m depth, ≤3 m;
cable engine controls speed/tension/length (the actuator set of a lay).

---

## 6. Open-Access Sources DOCX — annotated bibliography

Most implementation-relevant pointers (with URLs in the docx):
- **Zajac 1957** — steady lay/recovery theory (digested above).
- **Yoshizawa & Yabuta 1983** (IEEE JOE) — bottom tension at touchdown from
  negative slack; sea-trial validation. The slack/tension-control module.
- **Yang, Jeng & Zhou 2013** (Open Civil Eng J, open access) — semi-
  analytical 2-D lay tension with currents, ship motion, pay-out rate.
- **Pinto 1995** (UCL thesis, open access) — 2-D and 3-D low-tension cable
  dynamics: the key open reference for BU/bight-type manoeuvres where
  catenary theory fails.
- **Walton & Polachek 1960** — governing PDEs + finite differences for
  transient submerged cable motion (ancestor of lumped-mass codes).
- **Gatti-Bono & Perkins 2004** — elastica with tension/torsion/biaxial
  bending + seabed contact/friction (bight touchdown regime).
- Gaps flagged: no open source covers branching-unit deployment or final
  bight procedure directly — physics must be assembled from the above.

---

## Implications for V3 (summary)

1. **Steady lay with drag** is fully specified by Zajac: quadratic normal
   drag, `H = sqrt(2w/(C_D rho d))`, critical angle, `T_s = T_0 + w*h`,
   slack ↔ bottom tension. These become closed-form cross-checks for the
   3-D numerical engine, plus a fast "steady lay" answer mode.
2. **Shallow water**: keep V2's in-air segment treatment (already have
   air/water split) — JMSE quantifies why it matters (MBR error up to 59%).
3. **Chute friction** (capstan) is cheap to add and operationally critical.
4. **BU / final bight**: quasi-static stepping of a lumped-node model with
   Morison-type drag is the right level; no open reference does it
   directly, so validation is by limiting cases + internal consistency
   (+ the JMSE FEA seabed parameter set for contact/friction values).
5. **Typical drag coefficients**: C_D(normal) ≈ 1.1 smooth to 1.55 rough;
   tangential ~1–6% of normal weight term (often negligible for shape,
   noticeable for tension at high pay-out speeds).
