# RUDRA–FIREBOT UNIFIED PROPRIETARY SOFTWARE LICENSE
## Master License Agreement — RUFPSL-1.0
**Effective release:** 1.0  
**Project:** RUDRA FireBot Workspace  
**Covered workspace:** FireBot Application and FireBot Infrastructure  
**Classification:** Proprietary / Source-Available / Restricted Educational and Evaluation Use  
**Copyright:** © 2026 the respective FireBot Project authors and contributors. All rights reserved, subject to third-party rights.

---

# PREAMBLE

The RUDRA–FireBot workspace is a modular software engineering project comprising an application layer and a collection of separately identifiable infrastructure technologies. It is designed to support experimentation, simulation, software integration, sensor-data handling, communications, diagnostics, security-oriented messaging, telemetry, persistence, synchronization, and related robotics engineering activities.

The workspace is not a single indivisible program. It contains multiple components with different technical purposes, interfaces, operational boundaries, maturity levels, and potential deployment contexts. This License therefore establishes one master set of conditions and a collection of component-specific licensing schedules.

The master terms apply to all Covered Software unless a component schedule expressly narrows or supplements them. Each component schedule identifies its protected subject matter, permitted evaluation scope, restrictions, and component-specific disclaimers. A component schedule does not grant rights in any other component.

This is a proprietary, source-available license. It is not an open-source license and does not grant the broad rights associated with permissive or copyleft open-source licenses.

By accessing, downloading, copying, installing, executing, modifying, compiling, integrating, or otherwise using Covered Software, the Recipient agrees to the terms applicable to that activity, to the extent such agreement is enforceable under applicable law. If the Recipient does not accept the terms, the Recipient must not exercise rights that require acceptance.

---

# PART I — DEFINITIONS AND SCOPE

## 1. Definitions

**1.1 “Licensor”** means the copyright holder or holders of the particular Covered Software, or a person authorized by them to grant the relevant permission. The term does not imply that every contributor has assigned their copyright to one person or entity.

**1.2 “Recipient”** means an individual or legal entity accessing or using Covered Software.

**1.3 “Covered Software”** means only the original project-owned source code, object code, scripts, build files, tests, documentation, examples, schemas, specifications, interfaces, and other materials identified as part of the RUDRA–FireBot workspace, subject to the exclusions in this License.

**1.4 “Workspace”** means the repository or distribution containing the FireBot application and infrastructure packages, including later releases that expressly identify this License.

**1.5 “Component”** means a separately named package, subsystem, module, service, tool, application layer, or other independently identifiable part of the Workspace.

**1.6 “Component Schedule”** means a schedule under `LICENSES/COMPONENTS/` identifying a particular Component and its additional terms.

**1.7 “Evaluation Use”** means personal, classroom, laboratory, academic, demonstration, testing, or non-commercial research use that does not provide a commercial service or product and does not distribute Covered Software except as expressly permitted.

**1.8 “Commercial Use”** includes use in or for a product, service, paid deployment, commercial research engagement, revenue-generating activity, paid support offering, hosted service, consultancy, or other activity undertaken for commercial advantage.

**1.9 “Redistribution”** means publishing, transmitting, sublicensing, selling, mirroring, hosting for third-party access, bundling, or otherwise making Covered Software or a substantial portion of it available to another person.

**1.10 “Modification”** means changing, adapting, translating, porting, refactoring, or creating a derivative implementation of Covered Software.

**1.11 “Derivative Work”** has the meaning provided by applicable intellectual property law and includes a work that incorporates protectable expression from Covered Software.

**1.12 “Generated Material”** means code, schemas, reports, recordings, logs, binaries, protocol frames, or other outputs created through execution of Covered Software.

**1.13 “Third-Party Material”** means software, data, libraries, specifications, assets, or other materials not owned by the Licensor and included, referenced, or used by the Workspace.

**1.14 “RUDRA Language”** means the separately developed RUDRA programming language, its compiler, language specification, source format, and related materials, where distributed under the RUDRA Proprietary and Restricted Educational Software License, RPREL-1.0, or a later expressly designated license.

**1.15 “Safety-Critical Function”** means any function whose failure, delay, incorrect output, or unintended activation could reasonably contribute to death, personal injury, fire escalation, hazardous movement, environmental harm, or significant property damage.

## 2. Covered materials

Subject to the exclusions below, Covered Software includes:

a. `firebot-app` and its application modules, simulation environment, autonomy logic, motion guard, reporting, replay, configuration, tests, and documentation;

b. `firebot-infra` and its named infrastructure packages, including AKSHARA, ANANTA, SETU, DRISHTI, PRAVAH, KAVACH, SANGAM, TARANG, TANTU, LIPI, DHARA, SAMAYA, JALA, SPANDA, SETU-EDGE, and KSHITI;

c. shared event records, framing, command-line interfaces, benchmarks, integration glue, schemas, test fixtures, examples, and documentation;

d. component-specific interfaces, adapters, protocol definitions, configuration examples, and integration material authored as part of the Workspace;

e. future components only where the release or repository expressly identifies them as Covered Software under this License.

## 3. Excluded materials

This License does not claim ownership of:

a. Third-Party Material, including Python, the Python standard library, `cryptography`, SQLite, ClickHouse, operating systems, hardware SDKs, protocol standards, and other external dependencies;

b. independently authored material that is not a Derivative Work and does not incorporate protectable expression from Covered Software;

c. Recipient-created application data, original sensor recordings, original mission data, or original user-authored programs, except for project-owned templates or code embedded in them;

d. names, facts, ideas, algorithms, methods, interfaces, or concepts that are not protected by copyright or other applicable intellectual property rights;

e. materials expressly released under a different license by their respective rights holders.

Where a third-party dependency is included or required, its own license governs that dependency. Nothing in this License overrides or restricts rights that a third-party license independently grants.

## 4. Component separation

4.1 Each Component is licensed only within the scope of its own Component Schedule and the master terms.

4.2 Permission to evaluate one Component does not grant permission to use, modify, distribute, or commercially exploit another Component.

4.3 A Recipient may not avoid a restriction by renaming a package, moving files, changing import paths, splitting a repository, or combining components into a new distribution.

4.4 If a file is clearly attributable to a Component, the relevant Component Schedule applies to it. If no schedule clearly applies, the master terms apply.

4.5 Where a Component Schedule conflicts with the master terms, the more specific Component Schedule controls only for that Component and only to the extent of the express conflict. All other master terms remain in force.

---

# PART II — LIMITED GRANT

## 5. Evaluation permission

5.1 Subject to compliance with this License, the Licensor grants the Recipient a limited, personal, non-exclusive, non-transferable, non-sublicensable, revocable permission to:

a. access and download the Covered Software made publicly available by the project;

b. inspect and study the source code;

c. install and execute the Covered Software for Evaluation Use;

d. run the supplied tests, examples, demonstrations, and simulations;

e. create original programs or test inputs that interact with documented interfaces;

f. produce private modifications for personal evaluation, provided those modifications are not redistributed and do not violate third-party rights;

g. cite, describe, and discuss the project in academic work, subject to attribution and applicable quotation rules;

h. create limited excerpts reasonably necessary for classroom discussion, review, or criticism where permitted by applicable law.

5.2 The grant does not authorize public redistribution of the complete Workspace, any substantial Component, or a modified distribution.

5.3 The grant does not authorize Commercial Use.

5.4 The grant does not authorize deployment as a production control system, safety system, industrial controller, emergency response product, or commercial service.

5.5 Any permission not expressly granted is reserved.

## 6. Private modification

6.1 A Recipient may make private changes for Evaluation Use.

6.2 Private modifications must not be represented as official releases or verified project versions.

6.3 A Recipient may not publish, distribute, sell, sublicense, or provide public access to a modified compiler, application, infrastructure package, or substantial portion of Covered Software without separate written authorization.

6.4 Nothing in this section prohibits independent implementation of ideas, methods, or functionality that are not protected by applicable intellectual property rights, provided no protected expression or confidential material is unlawfully copied.

## 7. Academic and educational use

7.1 Classroom demonstrations, private laboratory exercises, academic evaluation, and non-commercial student projects are permitted within the limits of this License.

7.2 A Recipient may identify the project and describe its architecture, tests, results, and limitations in a report or presentation.

7.3 A Recipient may not submit the Licensor's source code or documentation as their own original authorship.

7.4 Academic citation does not grant permission to upload the complete repository or a substantial portion of its source code to another public repository, publication archive, package registry, or dataset.

7.5 A university, college, supervisor, evaluator, or educational institution does not obtain additional rights merely because the Recipient uses the Covered Software for a course or project.

---

# PART III — RESTRICTIONS

## 8. Prohibited redistribution

Unless separately authorized in writing, the Recipient shall not:

a. republish the Workspace or a substantial part of it;

b. create or maintain a public mirror or unofficial distribution;

c. upload Covered Software to package registries, code-hosting platforms, model-training datasets, source-code archives, or download portals;

d. bundle Covered Software into another product, framework, operating system image, firmware distribution, or commercial package;

e. provide hosted access to a copy of Covered Software for third parties;

f. distribute modified versions, forks, ports, or translations of Covered Software;

g. remove license notices and redistribute the affected materials;

h. sublicense, sell, rent, lease, or otherwise transfer rights in Covered Software;

i. distribute private keys, credentials, deployment secrets, or personal data accidentally present in a local working copy.

This section does not prohibit sharing a link to the official project repository or sharing limited excerpts where permitted by applicable law.

## 9. Prohibited commercialization

Without a separate commercial license, the Recipient shall not:

a. sell or license Covered Software;

b. charge for access to a hosted or packaged copy;

c. incorporate Covered Software into a product or service offered commercially;

d. use Covered Software to deliver paid engineering, integration, consulting, hosting, or support services;

e. use Covered Software as a material component of a revenue-generating deployment;

f. offer commercial support or warranties on behalf of the project;

g. claim that the Licensor has approved, certified, sponsored, or endorsed a commercial offering;

h. use the project identity to market an independently maintained commercial fork.

## 10. Prohibited relicensing and misrepresentation

10.1 The Recipient shall not replace, remove, or misrepresent this License.

10.2 The Recipient shall not label Covered Software as MIT, Apache-2.0, GPL, BSD, public domain, or otherwise unrestricted.

10.3 The Recipient shall not claim ownership of original project code, project documentation, or project-specific implementation details authored by others.

10.4 The Recipient shall not represent a fork, derivative, or independently maintained implementation as the official Workspace.

10.5 No trademark, logo, product identity, or endorsement right is granted.

## 11. Prohibited technical misuse

11.1 The Recipient shall not use Covered Software to conduct unlawful access, unauthorized interference, sabotage, destructive testing against systems without permission, or other unlawful activity.

11.2 The Recipient shall not use diagnostic, capture, transport, synchronization, or security components to access systems or data without authorization.

11.3 The Recipient shall not intentionally falsify test results, verification results, security status, safety status, or system reports in a manner that misleads another person about the behavior or assurance of a system.

11.4 This License does not prohibit lawful security research, responsible vulnerability analysis, or authorized testing within the scope of applicable law.

11.5 No provision grants permission to bypass access controls, extract another person's secrets, or interfere with third-party networks or devices.

## 12. No implied rights

No right is granted by implication, estoppel, exhaustion beyond mandatory law, or otherwise except as expressly stated. No patent license is granted except to the extent a non-waivable license arises under applicable law. No trademark license is granted.

---

# PART IV — COMPONENT LICENSING SCHEDULES

The following schedules form part of this License. Each schedule applies independently to the identified Component and supplements the master terms. A schedule does not create a separate unrestricted license.

## 13. Application Schedule — FIREBOT APPLICATION

13.1 Covered subject matter includes the FireBot application package, simulator, simulated world, simulated sensors, perception helper, mission state machine, motion guard, runner, replay tools, reporting, configuration, tests, and application documentation.

13.2 Evaluation permission is limited to local execution, simulation, academic demonstrations, private testing, and development of original programs that interact with documented interfaces.

13.3 The Recipient shall not represent simulation results as proof of real-world navigation, fire detection, rescue performance, collision avoidance, or operational reliability.

13.4 The motion guard is a software-level demonstration and is not a substitute for an independently engineered, validated, and tested low-level safety system.

13.5 No permission is granted to deploy the application as an autonomous emergency-response product or to represent it as a certified rescue system.

13.6 The Recipient shall not remove or misrepresent known limitations, including simulated sensor assumptions, odometry drift, incomplete fault detection, or the absence of fire suppression.

13.7 All restrictions in the master terms apply.

## 14. AKSHARA Schedule — Recording and Replay

14.1 Covered subject matter includes recorder queues, append-only recording segments, checksums, manifests, replay, replay harnesses, retention tools, and related interfaces.

14.2 The Recipient may evaluate recording and replay behavior using data they are authorized to process.

14.3 The Recipient shall not use AKSHARA to record communications, sensor data, personal information, or system activity without appropriate authority and legal basis.

14.4 Checksums and manifests are integrity mechanisms and do not independently establish authenticity, confidentiality, non-repudiation, or evidentiary admissibility.

14.5 The Recipient is responsible for access control, retention, encryption at rest, backup, deletion, and lawful handling of recordings.

14.6 No warranty is made that a recording is complete, tamper-proof, legally admissible, or sufficient to reconstruct every event.

## 15. ANANTA Schedule — Telemetry and Analytics

15.1 Covered subject matter includes privacy filters, bounded spools, telemetry connectors, batching, retries, deduplication, sinks, analytics schemas, dashboards, and reporting helpers.

15.2 The Recipient shall not assume that a default deny-list or pseudonymization function guarantees anonymity or legal compliance.

15.3 The Recipient is responsible for defining permitted data, lawful purpose, retention, access controls, jurisdictional requirements, and downstream processor obligations.

15.4 Analytics outputs must not be presented as complete or authoritative records unless independently verified.

15.5 The Recipient shall not use ANANTA to unlawfully collect, disclose, sell, or profile personal data.

15.6 Third-party database and analytics systems remain subject to their own terms.

## 16. SETU Schedule — Message Fabric

16.1 Covered subject matter includes message envelopes, schema registries, message nodes, deadlines, deduplication, retries, bounded queues, in-memory and network transports, stream support, and protocol integration.

16.2 Evaluation is permitted only between systems the Recipient owns or is authorized to test.

16.3 The Recipient is responsible for network exposure, authentication configuration, authorization policy, rate limits, schema validation, and deployment security.

16.4 Message delivery, retries, deadlines, and idempotency mechanisms do not guarantee exactly-once execution across arbitrary failures.

16.5 SETU is not a safety-rated control bus and must not be treated as one.

16.6 No permission is granted to transmit unauthorized commands to third-party devices or industrial networks.

## 17. DRISHTI Schedule — Sensor Quality and Observation State

17.1 Covered subject matter includes sensor observations, measurement specifications, units, frames, freshness, ordering, range checks, quality states, and flatline detection.

17.2 DRISHTI quality states are software assessments based on configured rules and available observations.

17.3 A VALID status does not establish physical sensor calibration, correct placement, measurement truth, or safety suitability.

17.4 The Recipient is responsible for calibration, validation, plausibility limits, environmental qualification, and independent sensor diagnostics.

17.5 No output from DRISHTI shall be treated as a certified measurement or sole basis for a Safety-Critical Function.

## 18. PRAVAH Schedule — Noncritical Task Runtime

18.1 Covered subject matter includes task state models, dependencies, timeout handling, bounded concurrency, recovery, and scheduling facilities.

18.2 PRAVAH is licensed for noncritical task orchestration and evaluation.

18.3 The Recipient shall not represent PRAVAH as a certified real-time operating system, safety scheduler, or deterministic actuator controller.

18.4 Scheduling, restart, timeout, and concurrency behavior must be independently validated for each deployment.

18.5 PRAVAH shall not be the sole mechanism for emergency stop, motor inhibition, suppression control, or other Safety-Critical Functions.

## 19. KAVACH Schedule — Identity, Signing, and Authorization

19.1 Covered subject matter includes device identity, cryptographic signing and verification, challenge-response, role policy, capability policy, key rotation, revocation, signed manifests, and PKI helpers.

19.2 KAVACH functionality is subject to correct key generation, storage, provisioning, rotation, revocation, trust configuration, and operational procedures.

19.3 A digital signature can support authenticity and integrity but does not by itself provide confidentiality.

19.4 The Recipient shall not claim that a system is secure solely because it uses KAVACH or a particular cryptographic algorithm.

19.5 Private keys and credentials must be protected and must not be committed to public source control.

19.6 Cryptographic implementation, protocol configuration, and deployment architecture require independent security review before production use.

19.7 No warranty is made that the system is free from vulnerabilities, side channels, implementation defects, or configuration errors.

## 20. SANGAM Schedule — Offline Synchronization

20.1 Covered subject matter includes peer journals, version vectors, conflict policies, expiring entries, synchronization services, and SETU integration.

20.2 The Recipient is responsible for selecting conflict policies appropriate to the data being synchronized.

20.3 Synchronization does not guarantee that replicas are always current, mutually consistent, or free from conflict.

20.4 Command-like, actuation-related, safety-related, or otherwise sensitive data must not be synchronized into a live control path without a separately designed and validated safety architecture.

20.5 No permission is granted to synchronize data belonging to systems or users without authorization.

## 21. TARANG Schedule — Passive Diagnostics and Capture

21.1 Covered subject matter includes passive capture, serial diagnostics, frame decoding, byte offsets, timing annotations, capture-loss reporting, regression fixtures, and offline fault injection.

21.2 Evaluation is limited to data and equipment the Recipient is authorized to inspect.

21.3 Passive capture tools must not be represented as guaranteeing complete capture or lossless observation.

21.4 Offline fault injection does not authorize live fault injection against third-party equipment.

21.5 Captured traces may contain credentials, personal information, proprietary protocol data, or other sensitive material. The Recipient is responsible for lawful handling and protection.

## 22. TANTU Schedule — Peripheral and Sensor Runtime

22.1 Covered subject matter includes sensor driver interfaces, simulated drivers, framed UART drivers, health and backoff logic, reset and close behavior, and configuration-built sensor bindings.

22.2 The described runtime is read-oriented in the current Workspace. This limitation must not be silently removed or misrepresented.

22.3 A driver reporting a successful read does not prove that the physical sensor is correctly calibrated or safe.

22.4 Hardware adapters require electrical, timing, environmental, and fault testing before deployment.

22.5 No permission is granted to use TANTU as a certified actuator or safety-control layer.

## 23. LIPI Schedule — Encoding and Compression

23.1 Covered subject matter includes variable-length integers, schema-based records, compression helpers, block files, random-access utilities, and benchmarks.

23.2 The Recipient is responsible for validating input sizes, decompression limits, malformed data handling, resource consumption, and compatibility.

23.3 Compression and checksums do not provide confidentiality or authenticity unless separate mechanisms are implemented.

23.4 No warranty is made that encoded data will remain compatible across versions unless a compatibility contract is expressly documented.

## 24. DHARA Schedule — Storage and Persistence

24.1 Covered subject matter includes the log-structured key-value store, SQLite baseline, shared interfaces, recovery behavior, and benchmarks.

24.2 The Recipient is responsible for backups, durability requirements, filesystem behavior, power-loss testing, access control, and retention.

24.3 Passing crash tests does not guarantee data survival under every hardware, filesystem, firmware, or power failure.

24.4 The Recipient shall not use DHARA as the sole storage protection for Safety-Critical Functions without independent qualification.

## 25. SAMAYA Schedule — Time and Causal Ordering

25.1 Covered subject matter includes hybrid logical clocks, Lamport clocks, vector clocks, offset estimation, uncertainty, causal delivery, and multi-device log merging.

25.2 Logical ordering is not equivalent to verified physical time.

25.3 Clock estimates may be uncertain, stale, or incorrect due to drift, network delay, device failure, or malicious behavior.

25.4 The Recipient shall not use logical timestamps as the sole basis for safety deadlines, collision timing, emergency sequencing, or other Safety-Critical Functions.

## 26. JALA Schedule — Mesh Routing

26.1 Covered subject matter includes link-state routing, topology state, route recovery, partition handling, TTL behavior, and deterministic network simulation.

26.2 Simulated routing results do not guarantee field performance, radio coverage, delivery, resilience, or availability.

26.3 The Recipient is responsible for network authorization, radio compliance, congestion, route policy, and security.

26.4 JALA does not provide a guarantee of secure routing or protection against malicious nodes unless separately implemented and verified.

## 27. SPANDA Schedule — Reliable Transport

27.1 Covered subject matter includes selective-repeat ARQ, acknowledgements, congestion adaptation, lossy-link simulation, and UDP adapter functionality.

27.2 SPANDA is experimental transport software unless a release expressly states otherwise.

27.3 The Recipient is responsible for congestion behavior, interoperability, resource limits, network security, and failure handling.

27.4 Simulated packet-loss performance does not establish production reliability or suitability for safety-critical communication.

## 28. SETU-EDGE Schedule — Industrial Protocol Edge

28.1 Covered subject matter includes Modbus RTU/TCP codecs, read-oriented client behavior, simulated server components, and TANTU driver integration.

28.2 The current read-oriented default must not be represented as a guarantee that every integration is incapable of writing or causing side effects.

28.3 The Recipient shall verify device addressing, register maps, access modes, electrical isolation, timeouts, and vendor-specific behavior.

28.4 Live testing is permitted only on equipment and networks the Recipient is authorized to operate.

28.5 No permission is granted to issue unauthorized writes or control commands to industrial equipment.

28.6 SETU-EDGE is not a certified industrial safety controller or safety-rated protocol implementation.

## 29. KSHITI Schedule — Kernel Simulation

29.1 Covered subject matter includes the deterministic microkernel simulator, scheduler model, IPC simulation, quotas, watchdog behavior, deadlock detection, and associated tests.

29.2 KSHITI is a simulator in the current Workspace and is not a production operating system.

29.3 Simulation behavior does not establish operating-system correctness, hard real-time guarantees, isolation guarantees, or hardware compatibility.

29.4 The Recipient shall not represent KSHITI as a certified kernel, hypervisor, or safety-critical runtime.

## 30. Shared Core Schedule

30.1 Covered subject matter includes shared events, framing, CRC and resynchronization utilities, common interfaces, CLI integration, benchmarks, and utilities not assigned to another schedule.

30.2 Shared interfaces may change as Components evolve.

30.3 The Recipient is responsible for version compatibility and integration testing.

30.4 Shared code does not grant rights to any separately licensed Component merely because that Component imports or uses it.

## 31. RUDRA Language Integration Schedule

31.1 This schedule covers only integration materials authored within the FireBot Workspace.

31.2 The RUDRA Language, its compiler, language specification, `.rudra` source format, and original compiler implementation are separate works.

31.3 Those separate works remain governed by RPREL-1.0 or the license expressly identified in their own repository or distribution.

31.4 This License does not replace, broaden, or supersede RPREL-1.0.

31.5 A Recipient must comply with both licenses when using materials covered by both.

31.6 If the RUDRA Language is not actually included in a particular FireBot distribution, this schedule does not imply that it is included.

---

# PART V — GENERATED MATERIAL, DATA, AND THIRD-PARTY RIGHTS

## 32. Generated Material

32.1 Subject to third-party rights and embedded project-owned templates, the Recipient retains rights in original data and original application content independently created by the Recipient.

32.2 This License does not automatically claim ownership of every log, recording, report, database row, simulation result, or generated file produced by execution.

32.3 Generated Material may contain project-owned templates, schemas, protocol definitions, or other Covered Software. The presence of Generated Material does not grant rights to redistribute those embedded portions.

32.4 The Recipient is responsible for reviewing generated files for secrets, personal information, proprietary data, and third-party content before sharing them.

## 33. Data protection

33.1 The Recipient must have an appropriate legal basis and authorization to collect, process, retain, transmit, or disclose personal or sensitive data.

33.2 The Recipient is responsible for privacy notices, consent where required, access control, retention, deletion, security, and cross-border transfer requirements.

33.3 A privacy filter, pseudonymization function, local recorder, or bounded metadata design is not a guarantee of legal compliance or anonymization.

33.4 The Recipient must not upload private recordings, credentials, device secrets, or personal data to public repositories.

## 34. Third-party software

34.1 Third-Party Material remains subject to its own license.

34.2 The Recipient must review and comply with applicable dependency licenses.

34.3 No third-party author is made a party to this License merely because their software is used by the Workspace.

34.4 Where an applicable third-party license grants rights that cannot lawfully be restricted by this License, those rights remain unaffected.

34.5 If a dependency license conflicts with a project distribution term, the Recipient must comply with the applicable dependency license and must not assume that this License grants rights in that dependency.

---

# PART VI — SAFETY, SECURITY, AND OPERATIONAL DISCLAIMERS

## 35. Experimental and development status

35.1 The Workspace is a software engineering and educational project.

35.2 Components may be incomplete, experimental, unverified, or subject to change.

35.3 Passing unit tests, integration tests, simulation scenarios, benchmarks, or self-tests does not prove field safety, correctness, security, or fitness for a particular deployment.

35.4 The Recipient must independently assess all deployment risks.

## 36. No safety certification

36.1 The Workspace is not represented as certified under any functional safety, industrial safety, medical, aviation, firefighting, emergency response, or other regulated safety standard.

36.2 No Component is warranted to be suitable as the sole controller, monitor, interlock, or protective mechanism for a Safety-Critical Function.

36.3 Emergency stop, motor power isolation, braking, suppression interlocks, thermal protection, battery protection, and other protective functions must be implemented and validated independently where relevant.

36.4 Software guards, sensor-quality states, signatures, logs, watchdogs, simulation, and protocol checks are not substitutes for validated hardware protection and system-level hazard analysis.

## 37. Fire and rescue limitation

37.1 The Workspace must not be represented as guaranteeing fire detection, fire extinguishment, human detection, human rescue, safe navigation through smoke, or safe operation in hazardous environments.

37.2 Any fire, rescue, or emergency-response use requires separate engineering, hazard analysis, regulatory review, testing, operational procedures, and qualified oversight.

37.3 The current simulation and application materials do not establish that a physical robot can safely enter a burning structure or rescue a person.

## 38. Security limitations

38.1 Security features are subject to implementation, configuration, key management, operational, and environmental risks.

38.2 Cryptographic signing does not inherently encrypt messages.

38.3 Local recording and checksums do not inherently prevent a privileged attacker from altering or deleting data.

38.4 Network transports, synchronization, telemetry, and diagnostics require deployment-specific security review.

38.5 The Recipient must not describe the Workspace as unhackable, invulnerable, tamper-proof, or guaranteed secure.

---

# PART VII — WARRANTY AND LIABILITY

## 39. Disclaimer of warranty

TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, THE COVERED SOFTWARE IS PROVIDED “AS IS” AND “AS AVAILABLE”, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, WHETHER EXPRESS, IMPLIED, STATUTORY, OR OTHERWISE.

THIS INCLUDES, WITHOUT LIMITATION, WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, TITLE, NON-INFRINGEMENT, ACCURACY, AVAILABILITY, RELIABILITY, SECURITY, COMPATIBILITY, DATA INTEGRITY, ERROR-FREE OPERATION, AND RESULTS.

THE LICENSOR DOES NOT WARRANT THAT THE SOFTWARE WILL OPERATE WITHOUT INTERRUPTION, THAT DEFECTS WILL BE CORRECTED, THAT OUTPUTS WILL BE ACCURATE, OR THAT ANY COMPONENT WILL MEET A RECIPIENT'S REQUIREMENTS.

## 40. Limitation of liability

TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, THE LICENSOR AND CONTRIBUTORS SHALL NOT BE LIABLE FOR INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, PUNITIVE, OR CONSEQUENTIAL LOSS ARISING OUT OF OR IN CONNECTION WITH THE COVERED SOFTWARE OR THIS LICENSE.

THIS INCLUDES LOSS OF DATA, LOSS OF PROFITS, LOSS OF REVENUE, BUSINESS INTERRUPTION, EQUIPMENT DAMAGE, SYSTEM FAILURE, NETWORK FAILURE, SECURITY INCIDENTS, PERSONAL INJURY, PROPERTY DAMAGE, AND FAILURE OF A ROBOTIC OR EMBEDDED SYSTEM.

NOTHING IN THIS LICENSE EXCLUDES OR LIMITS LIABILITY THAT CANNOT LAWFULLY BE EXCLUDED OR LIMITED UNDER APPLICABLE LAW.

## 41. Recipient responsibility

The Recipient is responsible for:

a. determining whether use is lawful;

b. obtaining required permissions and approvals;

c. evaluating hardware and software compatibility;

d. validating generated outputs and integration behavior;

e. protecting credentials, keys, recordings, and personal data;

f. implementing independent safety protections;

g. maintaining backups and recovery procedures;

h. ensuring that deployment is supervised and appropriate to its risk.

---

# PART VIII — TERM, TERMINATION, AND ENFORCEMENT

## 42. Term

This License applies for as long as the Recipient exercises rights granted under it, subject to termination and applicable law.

## 43. Termination

43.1 The permission granted under this License terminates automatically upon a material breach, to the extent permitted by applicable law.

43.2 Upon termination, the Recipient must cease activities that rely on the terminated permission and must stop unauthorized redistribution or commercial use.

43.3 Termination does not extinguish rights that the Recipient independently holds in original works or third-party materials.

43.4 A Licensor may restore permission through an express written reinstatement.

## 44. Remedies

The Licensor may pursue remedies available under applicable law for infringement, unauthorized distribution, breach of contract, or other unlawful conduct. No remedy is guaranteed, and no provision creates a right to damages or injunctive relief beyond what applicable law permits.

## 45. No obligation to maintain

The Licensor has no obligation to provide support, updates, security patches, documentation, compatibility changes, hosting, or continued availability.

---

# PART IX — GOVERNING LAW AND GENERAL TERMS

## 46. Governing law

This License is intended to be interpreted under the laws of India, subject to mandatory provisions of applicable law.

## 47. Jurisdiction

Subject to applicable law and the rules governing jurisdiction, disputes shall be brought before courts of competent jurisdiction in Maharashtra, India.

## 48. Severability

If a provision is held invalid or unenforceable, it shall be modified to the minimum extent necessary to make it enforceable, where legally permitted. The remaining provisions shall continue in effect.

## 49. No waiver

Failure to enforce a provision is not a waiver of that provision or any other right.

## 50. Assignment

The Recipient may not assign or transfer permissions under this License without written authorization. The Licensor may transfer rights only to the extent it owns or is authorized to transfer them.

## 51. Entire agreement

This License and its Component Schedules constitute the licensing terms for Covered Software, except where a separate written agreement or third-party license expressly governs particular materials.

## 52. Amendments and versions

52.1 This document is RUFPSL-1.0.

52.2 A later release may identify a later license version.

52.3 No later version automatically changes the terms governing an earlier copy unless the applicable rights holder expressly states otherwise or the parties agree.

52.4 The Recipient should retain the license version distributed with the copy they use.

## 53. Interpretation

Headings are for convenience. “Including” means “including without limitation.” Singular terms include the plural where appropriate. Nothing in this License is intended to waive non-waivable statutory rights.

---

# FINAL DECLARATION

The RUDRA–FireBot Workspace is a modular engineering software project. Its application, infrastructure systems, component implementations, interfaces, and documentation are made available for limited evaluation and educational use under this proprietary, source-available license.

Each named Component is subject to its own schedule. Permission for one Component does not grant permission for another. No commercial, redistribution, sublicensing, certification, safety, or endorsement right is implied.

**ALL RIGHTS NOT EXPRESSLY GRANTED ARE RESERVED.**

**RUDRA–FIREBOT UNIFIED PROPRIETARY SOFTWARE LICENSE**  
**RUFPSL-1.0**  
© 2026 the respective FireBot Project authors and contributors. All rights reserved, subject to third-party rights.
