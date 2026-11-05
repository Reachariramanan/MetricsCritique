# Entity list: what the pipeline extracts, and how the domain entities map to it

**No pipeline code or config was changed.** This file only maps two lists to each other. Nothing in Part B or Part C is extracted today.

Two lists are compared:

1. **Extracted today.** The fields of `cfg/fields_v3.yaml` (`fields_v3.4`) and the frame (`framing_v2`). They exist to compute the severity score.
2. **Domain entity types** of `EVENT_GROUPING_ENTITY_LIST.md`, taken from the 98 types of `ENTITY_MAP_master_F.md`. They are a proposal to decide whether two articles report the same event. Its own caveats say the domain sets come from 10 articles per domain and are for owner review.

Paths are relative to `news-entity-extract/`.

**Status legend** (used for every domain entity type):

| Status | Meaning | Rule to avoid duplicates |
|---|---|---|
| **Covered** | A field extracted today holds the same fact. | Do not add. Keep the type only as an alias of that field. |
| **Partly covered** | A field holds a coarser, class-level or bucket-limited version (a count, a label, or a class instead of a name). | Do not add a parallel field. Extend or attach the named detail to the existing field. |
| **New** | Nothing extracted today holds it. | Add it as a new entry (once, even if it appears in several domains). |

---

## 0. Result of the overlap analysis

- The grouping list names **156 type entries** across core and D01–D09, but only **95 unique types**. Counting each type once removes 61 repeats (for example `ACT_GOVT` appears in 5 domains, `HRM_KILLED` in 3, `AMT_MONEY` in 4).
- The map has **98 types**; 3 of them are not in the grouping list (see section 7).

| Status | Unique types | Share |
|---|---:|---:|
| Covered | 8 | 8% |
| Partly covered | 32 | 34% |
| New | 55 | 58% |
| **Total** | **95** | |

What this means:

1. **Only 8 types are true duplicates.** They are the harm counts and dates: `EVT_DATE`, `HRM_PROTESTERS`, `HRM_KILLED`, `HRM_WOUNDED`, `HRM_KIDNAPPED`, `HRM_DAMAGE`, `HRM_AFFECTED`, `HRM_DISPLACED`. Add nothing for these.
2. **32 types are partly covered.** The pipeline holds the class, the count or the label; the list wants the named instance. Two patterns repeat: *class vs name* (`target_class` vs `ACT_TARGET`, `INF_*`, `CYB_TARGET`) and *label vs system* (`weapon_class` vs `WPN_*`).
3. **55 types are new.** Most are names: actors (`ACT_*`), law and policy (`LEG_*`), named events and operations (`EVT_NAMED_OP`, `EVT_HAZARD`), places (`LOC_*`), and cyber artefacts (`CYB_*`). The pipeline extracts none of these, because its fields feed a score, not an identity.
4. **18 pipeline fields have no counterpart** in the grouping list (section 2, last table). They measure severity (outage, damage, dependency, CIA triad, breach depth) and the list does not need them.
5. **Same field, different shape.** The pipeline stores one value per field for the main event, with a status and a verbatim quote. The list wants entity mentions with a canonical id, a role and a confidence (section 5).
6. **Bucket versus domain.** The list is organised by domain (D01–D09); the pipeline's bucket-specific fields are organised by bucket. A domain-level type can therefore be unreachable. Example: `HRM_ARRESTED` is wanted in D06 and D09, but `arrests` exists only in the Political bucket (section 1).

---

## 1. Domain to bucket map

The pipeline routes each article to a bucket by its taxonomy domain (`cfg/buckets.yaml`). Bucket-specific fields exist only for that bucket.

| Domain | Name | Bucket | Bucket fields available for this domain |
|---|---|---|---|
| D01 | Conflict, Coercion & Security | Political-Civil | `arrests`, `violence_level`, `institutional_effect`, `participants`, `coercion_measures`, `factions_count` |
| D02 | Political, Diplomatic & Governance | Political-Civil | `arrests`, `violence_level`, `institutional_effect`, `participants`, `coercion_measures`, `factions_count` |
| D03 | Economic, Trade & Infrastructure | Economic | `capacity_lost_pct`, `affected_value`, `affected_value_total`, `price_change_pct`, `dependency`, `replacement_difficulty` |
| D04 | Maritime Security & Operations | Economic | `capacity_lost_pct`, `affected_value`, `affected_value_total`, `price_change_pct`, `dependency`, `replacement_difficulty` |
| D05 | Military & Defence | Political-Civil | `arrests`, `violence_level`, `institutional_effect`, `participants`, `coercion_measures`, `factions_count` |
| D06 | Borders, Territory & Transboundary Resources | Terrorism/Breach | `operation_stage`, `weapon_class`, `coordination`, `breach_depth`, `facilitation` |
| D07 | Technology, Cyber & Hybrid Security | Cyber | `confidentiality`, `integrity`, `availability`, `subsequent_impact`, `attack_depth`, `recovery_state` |
| D08 | Humanitarian & Civilian Security | Political-Civil | `arrests`, `violence_level`, `institutional_effect`, `participants`, `coercion_measures`, `factions_count` |
| D09 | Terrorism, Insurgency & Internal Security | Terrorism/Breach | `operation_stage`, `weapon_class`, `coordination`, `breach_depth`, `facilitation` |

---

## 2. Part A: extracted today

Generated from `cfg/fields_v3.yaml`, `harness/skills/news-frame/framing.yaml`, `cfg/agent_loop.yaml` and `cfg/features_v0.yaml`. Each field also shows its counterpart in the grouping list.

The pipeline does not tag names of people, organisations or places. It extracts typed **fields about the main event**: counts, amounts, dates and labels from fixed lists, each with a `status` and a verbatim `evidence` quote.

| Group | Fields |
|---|---:|
| Frame (every article) | 4 |
| Common (every bucket) | 18 |
| Political-Civil bucket only | 6 |
| Economic bucket only | 6 |
| Cyber bucket only | 6 |
| Terrorism/Breach bucket only | 5 |

Each article gets the frame, the common fields and its own bucket's fields: 24 (Political), 24 (Economic), 24 (Cyber), 23 (Terror), plus the frame.

**Statuses:**

- `reported`: The article gives a value for this field.
- `none`: The article clearly tells that there was none (for example "no injuries").
- `not_mentioned`: The article gives nothing about this field.
- `unknown`: The article tells that the value is not known yet or is not clear (for example "the number of dead is not known").

**Qualifiers** (counts and quantities): `exact`, `at_least`, `approx`, `at_most`.

**Kinds:** `count` = whole number; `quantity` = number + unit; `enum` = one label from a fixed list; `date` = YYYY, YYYY-MM or YYYY-MM-DD; `text` = free sentence.

**Scoring node legend:**

| Node | Feeds | Meaning |
|---|---|---|
| `H`, `O`, `A`, `E` | M (material impact) | Harm to people, service disruption, asset damage, economic loss |
| `P`, `Gx`, `K` | X (reach and exposure) | Exposed count, geographic extent, target criticality |
| `V`, `S`, `T`, `C` | D (event dynamics) | Velocity, spread, duration, containment |
| `pol_*` | PCPI (Political) | Violence, institutions, places, participants, coercion, factions |
| `eco_*` | EDMI (Economic) | Loss, capacity, dependency, price, replacement |
| `cy_*`, `CIA` | CIII (Cyber) | Confidentiality, integrity, availability, downstream, recovery, depth |
| `tb_*` | TSBI (Terror) | Weapon, stage, network, breach, facilitation |

### 2.1 Frame fields (call 1, every article)

| Field | Type | Meaning | Grouping-list counterpart |
|---|---|---|---|
| `main_event` | text | One sentence. The one development that the article reports now (who did what, to whom, where), and what occurred. All field calls use this event. | none (pipeline-only) |
| `article_kind` | enum | The type of report that the article gives about its main event. | `EVT_MODE`, `EVT_OUTCOME` (partly covered) |
| `incident_date` | date | The date of the incident that the facts of the article (counts, harm, damage) are about. For a situation that continues, use its start date if the article gives it. For a period, use the END of the period. Give the date as exactly as the article gives it (YYYY-MM-DD, YYYY-MM or YYYY). | `EVT_DATE` (covered) |
| `development_date` | date | The date of the development that the article reports now (the ruling, the memorial, the latest attack, the statement, the scheduled date). For a period, use the END of the period. Give the date as exactly as the article gives it. | `EVT_DATE` (covered) |

`article_kind` labels:

- `new_incident`: A recent incident that the article reports as news (attack, protest, outage, breach, accident, arrest, decision, announcement, data release).
- `ongoing_update`: A new development in a situation that started before and continues now (war, crisis, campaign, outbreak, investigation).
- `legal_proceeding`: A charge, indictment, trial, plea, conviction, sentence, court ruling or lawsuit. This also applies when it is about an older incident.
- `retrospective`: A commemoration, anniversary, retrospective report, inquiry result or study about an incident that is finished.
- `forthcoming`: A planned, scheduled or threatened event that did not occur yet.
- `analysis_or_other`: Commentary, analysis, explainer, listing, sport or entertainment with no main development.

### 2.2 Which call extracts which field

| Call | Common fields | Political | Economic | Cyber | Terror |
|---|---|---|---|---|---|
| `actors` | `fatalities`, `injuries`, `missing`, `displaced`, `hostages`, `target_class` | `violence_level`, `participants`, `arrests`, `coercion_measures`, `factions_count` | – | `attack_depth`, `confidentiality`, `integrity` | `operation_stage`, `weapon_class`, `coordination`, `breach_depth`, `facilitation` |
| `disruption` | `service_disruption`, `asset_damage`, `economic_loss`, `disruption_duration`, `recovery_pct`, `trend` | `institutional_effect` | `capacity_lost_pct`, `affected_value`, `affected_value_total`, `price_change_pct` | `availability`, `recovery_state` | – |
| `reach` | `exposed_count`, `geo_extent`, `affected_places_count`, `direction`, `certainty`, `attribution` | – | `dependency`, `replacement_difficulty` | `subsequent_impact` | – |

### 2.3 Common fields (all four buckets)

#### `attribution`

- **Type:** enum
- **Extracted in call:** reach
- **Used in scoring by:** outside S (directness)
- **Meaning:** The main source of the facts in the article.
- **Grouping-list counterpart:** `EVT_CLAIM` (partly covered)
- **Allowed labels:**
  - `official_source`: A statement from a government, police, military, company or other official body.
  - `journalist_or_witness`: Direct report from the journalist or from named witnesses.
  - `claimed_by_party`: A claim from a party with an interest (perpetrator, protest group, hacker group).
  - `anonymous_or_unverified`: Anonymous sources, social media or reports that nobody has confirmed.

#### `certainty`

- **Type:** enum
- **Extracted in call:** reach
- **Used in scoring by:** outside S (directness)
- **Meaning:** How sure the article is that the main event occurred.
- **Grouping-list counterpart:** `EVT_MODE` (partly covered), `EVT_CLAIM` (partly covered)
- **Allowed labels:**
  - `asserted`: The article tells it as a fact.
  - `alleged_or_reported`: The article tells it as alleged, reported or claimed.
  - `speculative`: The article tells it as possible, as a rumour or as a prediction.

#### `affected_places_count`

- **Type:** count
- **Extracted in call:** reach
- **Used in scoring by:** Gx, pol_F
- **Meaning:** The number of different cities, towns or sites where the main event occurred or had an effect.
- **Grouping-list counterpart:** `LOC_POINT` (partly covered), `LOC_ADMIN` (partly covered)

#### `geo_extent`

- **Type:** enum
- **Extracted in call:** reach
- **Used in scoring by:** Gx
- **Meaning:** How far the effect of the main event goes. Find the places where persons, services, trade or security get an effect. It is not only the place where the event occurred.
- **Grouping-list counterpart:** `LOC_POINT` (partly covered), `LOC_ADMIN` (partly covered), `LOC_COUNTRY` (partly covered)
- **Allowed labels:**
  - `single_site`: One building, facility, site or road section.
  - `single_city`: One city, town or district.
  - `multiple_cities`: Two or more cities or districts.
  - `province_wide`: Most of a province or state, or two or more provinces.
  - `national`: All of the country.
  - `multi_country`: Two or more countries.

#### `target_class`

- **Type:** enum
- **Extracted in call:** actors
- **Used in scoring by:** K
- **Meaning:** The main target of the event, or the main object that the event hit.
- **Grouping-list counterpart:** `ACT_TARGET` (partly covered), `INF_PUBLIC` (partly covered), `INF_ENERGY` (partly covered), `INF_TRANSPORT` (partly covered), `INF_DIGITAL` (partly covered), `INF_FINANCIAL` (partly covered), `INF_MILITARY` (partly covered), `INF_SUPPLY_CHAIN` (partly covered), `EVT_ELECTION` (partly covered), `CYB_TARGET` (partly covered)
- **Allowed labels:**
  - `none`: No target or object.
  - `general_public`: Ordinary civilians, residents or consumers.
  - `public_gathering`: A crowd, rally, market, festival or other gathering.
  - `private_business`: An ordinary company, shop or private property.
  - `local_government`: A municipal or district authority or a local office.
  - `national_government`: A national government, parliament, ministry or national institution.
  - `security_forces`: Police, paramilitary or security personnel or posts.
  - `military`: Military forces, bases or equipment.
  - `energy_infrastructure`: A power grid, power plant, pipeline, refinery or fuel supply.
  - `transport_infrastructure`: A port, airport, railway, highway, shipping route or chokepoint.
  - `telecom_or_digital`: Telecom networks, internet services, data centres or cloud platforms.
  - `financial_system`: Banks, exchanges, payment systems or a central bank.
  - `health_facility`: A hospital, clinic or health system.
  - `education_facility`: A school, university or students.
  - `religious_site`: A temple, mosque, church or other religious site.
  - `election_system`: An election process, voters or election equipment.
  - `senior_official`: A head of state or government, a minister or other senior official.
  - `diplomatic_mission`: An embassy, consulate or diplomats.
  - `nuclear_or_cbrn_site`: A nuclear facility or a chemical or biological site.

#### `direction`

- **Type:** enum
- **Extracted in call:** reach
- **Used in scoring by:** outside S (signed auxiliary)
- **Meaning:** The effect of the main event on stability and security.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `strongly_harmful`: Severe harm or loss of stability.
  - `harmful`: Harm or loss of stability.
  - `neutral`: No clear harmful or helpful effect.
  - `beneficial`: Better stability, security or cooperation.
  - `strongly_beneficial`: A large improvement, for example a peace agreement or a large restoration.

#### `fatalities`

- **Type:** count
- **Extracted in call:** actors
- **Used in scoring by:** H
- **Meaning:** Persons that the main event killed (violence, accident, disaster). Do not count deaths from illness or natural causes. Do not count deaths in earlier or background incidents.
- **Grouping-list counterpart:** `HRM_KILLED` (covered), `HRM_CIVILIAN` (partly covered), `HRM_SF_CASUALTY` (partly covered), `HRM_NEUTRALISED` (partly covered)

#### `injuries`

- **Type:** count
- **Extracted in call:** actors
- **Used in scoring by:** H
- **Meaning:** Persons that the main event injured.
- **Grouping-list counterpart:** `HRM_WOUNDED` (covered), `HRM_CIVILIAN` (partly covered), `HRM_SF_CASUALTY` (partly covered)

#### `missing`

- **Type:** count
- **Extracted in call:** actors
- **Used in scoring by:** H
- **Meaning:** Persons that are missing because of the main event.
- **Grouping-list counterpart:** none (pipeline-only field)

#### `displaced`

- **Type:** count
- **Extracted in call:** actors
- **Used in scoring by:** H
- **Meaning:** Persons that left their homes or were evacuated because of the main event.
- **Grouping-list counterpart:** `HRM_DISPLACED` (covered)

#### `hostages`

- **Type:** count
- **Extracted in call:** actors
- **Used in scoring by:** H
- **Meaning:** Persons that were taken as hostages or abducted in the main event.
- **Grouping-list counterpart:** `HRM_KIDNAPPED` (covered)

#### `service_disruption`

- **Type:** enum
- **Extracted in call:** disruption
- **Used in scoring by:** O
- **Meaning:** A stop or decrease of services or operations that the main event caused.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `none`: No disruption.
  - `minor_degradation`: Small delays or a small decrease.
  - `partial_outage`: Important services are partly not available.
  - `major_outage`: A large service is not available.
  - `critical_broadly_unavailable`: Critical or essential services are not available in a large area.

#### `disruption_duration`

- **Type:** quantity
- **Extracted in call:** disruption
- **Used in scoring by:** T
- **Meaning:** How long the disruption or the event lasted. The unit must be minutes, hours, days, weeks or months.
- **Grouping-list counterpart:** none (pipeline-only field)

#### `asset_damage`

- **Type:** enum
- **Extracted in call:** disruption
- **Used in scoring by:** A
- **Meaning:** Physical damage or system damage to assets.
- **Grouping-list counterpart:** `HRM_DAMAGE` (covered)
- **Allowed labels:**
  - `none`: No damage.
  - `minor`: Small damage.
  - `moderate`: Medium damage to some assets.
  - `severe`: Severe damage to important assets.
  - `destroyed_or_critical`: Assets destroyed, or critical assets that do not operate.

#### `economic_loss`

- **Type:** quantity
- **Extracted in call:** disruption
- **Used in scoring by:** E, eco_L
- **Meaning:** Money lost, or the cost, that the event caused. The unit is a currency code (USD, EUR, INR, GBP, CNY, JPY, ...).
- **Grouping-list counterpart:** `AMT_MONEY` (partly covered)

#### `exposed_count`

- **Type:** quantity
- **Extracted in call:** reach
- **Used in scoring by:** P
- **Meaning:** The number of persons, customers, organisations, data records or devices that the harm or the disruption of the main event hit (for example customers without power, records that leaked, users with an effect). Do not count persons who were killed, injured, missing, displaced or taken as hostages (other fields count them). Do not count attendees, tests, claims, sales, grants, tourists, statistics or market figures. The unit must be people, customers, organisations, records or devices.
- **Grouping-list counterpart:** `HRM_AFFECTED` (covered), `CYB_DATA` (partly covered)

#### `trend`

- **Type:** enum
- **Extracted in call:** disruption
- **Used in scoring by:** V, S, C
- **Meaning:** How the main event changes at the time of the report.
- **Grouping-list counterpart:** `EVT_OUTCOME` (partly covered), `EVT_ESCALATION` (partly covered)
- **Allowed labels:**
  - `escalating`: The event gets more intense or more severe.
  - `spreading`: The event goes to more places, systems, actors or sectors.
  - `stable`: The event continues with no clear change.
  - `contained`: The event does not spread more, but it is not finished.
  - `recovering`: Services, operations or persons come back or are released.
  - `resolved`: The event is finished, restored or settled.

#### `recovery_pct`

- **Type:** quantity
- **Extracted in call:** disruption
- **Used in scoring by:** C
- **Meaning:** The percentage of service, capacity or persons that came back. The unit is percent.
- **Grouping-list counterpart:** `AMT_PERCENT` (partly covered)

### 2.4 Bucket-specific fields

#### Political-Civil bucket (`political`, domains D01, D02, D05, D08)

##### `arrests`

- **Type:** count
- **Extracted in call:** actors
- **Used in scoring by:** pol_C
- **Meaning:** Persons that the authorities arrested or detained because of the main event.
- **Grouping-list counterpart:** `HRM_ARRESTED` (partly covered)

##### `violence_level`

- **Type:** enum
- **Extracted in call:** actors
- **Used in scoring by:** pol_V
- **Meaning:** The highest level of confirmed violence in the main event.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `peaceful`: The article clearly tells that it was peaceful. No violence.
  - `limited_confrontation`: Damage to property at some places, or a small physical confrontation.
  - `nonlethal_clashes`: Clashes with injuries but no deaths.
  - `serious_or_lethal`: Serious violence or confirmed lethal force.
  - `mass_casualty`: Many casualties, or very severe violence that continues.

##### `institutional_effect`

- **Type:** enum
- **Extracted in call:** disruption
- **Used in scoring by:** pol_I
- **Meaning:** The effect on the government or on political institutions.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `no_interruption`: The article confirms that there was no interruption.
  - `minor_interference`: Small interference with administration.
  - `temporary_suspension`: A temporary stop of an important function of an institution.
  - `severe_disruption`: Severe disruption of important operations of an institution.
  - `breakdown`: An institution stops, or it fully loses an essential function.

##### `participants`

- **Type:** count
- **Extracted in call:** actors
- **Used in scoring by:** pol_P
- **Meaning:** The number of persons that take part in a protest, strike, rally, march, blockade or other collective political action. Do not count persons at meetings, conferences or ceremonies. Do not count members of a body, staff or objects.
- **Grouping-list counterpart:** `HRM_PROTESTERS` (covered)

##### `coercion_measures`

- **Type:** enum
- **Extracted in call:** actors
- **Used in scoring by:** pol_C
- **Meaning:** The strongest measure of force by the authorities.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `none`: No measure of force.
  - `routine_policing`: Police are present, or routine control.
  - `arrests_or_dispersal`: Arrests, detentions, tear gas or dispersal of a crowd.
  - `emergency_powers`: A curfew, an emergency declaration, an internet shutdown or bans.
  - `lethal_force_or_mass_detention`: Live fire, lethal force or detention of many persons.

##### `factions_count`

- **Type:** count
- **Extracted in call:** actors
- **Used in scoring by:** pol_A
- **Meaning:** The number of different organised groups or security actors in the main event.
- **Grouping-list counterpart:** `ACT_ARMED_FORCES` (partly covered)

#### Economic bucket (`economic`, domains D03, D04)

##### `capacity_lost_pct`

- **Type:** quantity
- **Extracted in call:** disruption
- **Used in scoring by:** eco_T
- **Meaning:** The percentage of capacity, output or throughput that was lost. The unit is percent.
- **Grouping-list counterpart:** `AMT_PERCENT` (partly covered)

##### `affected_value`

- **Type:** quantity
- **Extracted in call:** disruption
- **Used in scoring by:** eco_L
- **Meaning:** The money value of the trade, output or assets that the event hit. The unit is a currency code.
- **Grouping-list counterpart:** `AMT_MONEY` (partly covered)

##### `affected_value_total`

- **Type:** quantity
- **Extracted in call:** disruption
- **Used in scoring by:** eco_L (denominator)
- **Meaning:** The total that affected_value is a part of. Use it only when the article gives this total (for example the total trade for one year). The unit is a currency code.
- **Grouping-list counterpart:** `AMT_MONEY` (partly covered)

##### `price_change_pct`

- **Type:** quantity
- **Extracted in call:** disruption
- **Used in scoring by:** eco_P
- **Meaning:** A price change that the event caused. The unit is percent. Use a negative value for a decrease.
- **Grouping-list counterpart:** `AMT_PERCENT` (partly covered)

##### `dependency`

- **Type:** enum
- **Extracted in call:** reach
- **Used in scoring by:** eco_K
- **Meaning:** How much others need the asset, route or commodity that the event hit.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `ordinary`: An ordinary asset. Nobody needs it specially.
  - `regional`: Important for a region.
  - `national`: Important for all of a country.
  - `strategic_or_single_source`: A strategic commodity or the only source.
  - `global_chokepoint`: A global chokepoint, or a supply that the world needs.

##### `replacement_difficulty`

- **Type:** enum
- **Extracted in call:** reach
- **Used in scoring by:** eco_R
- **Meaning:** How easily other sources can replace the lost capacity or supply.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `easy`: Alternatives are easily available.
  - `some_alternatives`: Some alternatives, with cost or delay.
  - `limited_alternatives`: Few alternatives, with a long delay.
  - `none_short_term`: No alternative in the short term.

#### Cyber bucket (`cyber`, domains D07)

##### `confidentiality`

- **Type:** enum
- **Extracted in call:** actors
- **Used in scoring by:** cy_C
- **Meaning:** Access to data, or data that attackers took out.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `none`: Nobody got access to data.
  - `limited_nonsensitive`: A small quantity of data that is not sensitive.
  - `moderate`: Some sensitive data, or many records that are not sensitive.
  - `sensitive_exfiltrated`: Attackers took out sensitive personal, financial or business data.
  - `massive_or_highly_sensitive`: Very large or very sensitive data (classified, health, many credentials).

##### `integrity`

- **Type:** enum
- **Extracted in call:** actors
- **Used in scoring by:** cy_I
- **Meaning:** Changes to data or to the state of systems.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `none`: No change.
  - `limited`: A small change.
  - `moderate`: Some systems or data were changed.
  - `major`: Important systems or data were changed or encrypted.
  - `critical`: Critical systems or control systems were changed.

##### `availability`

- **Type:** enum
- **Extracted in call:** disruption
- **Used in scoring by:** cy_A
- **Meaning:** Systems or services that are not available.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `none`: No effect on availability.
  - `minor_degradation`: A small decrease.
  - `partial`: Important services are partly not available.
  - `major`: A large service is not available.
  - `critical_broadly_unavailable`: An essential or critical service is not available in a large area.

##### `subsequent_impact`

- **Type:** enum
- **Extracted in call:** reach
- **Used in scoring by:** cy_S
- **Meaning:** How far the attack went to other systems or organisations that use the first one.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `none`: No effect on other systems or organisations.
  - `single_downstream`: One other system or organisation.
  - `multiple_downstream`: Two or more other systems or organisations.
  - `sector_wide`: A full sector or supply chain.

##### `attack_depth`

- **Type:** enum
- **Extracted in call:** actors
- **Used in scoring by:** cy_Dp
- **Meaning:** The deepest level of access that the attacker got.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `attempt_only`: Only an attempt or a scan. No access.
  - `user_account`: User accounts or devices of users.
  - `admin_or_server`: Administrator access or servers.
  - `network_wide`: Access to a domain or to the full network.
  - `control_systems`: Industrial control systems, or two or more networks.

##### `recovery_state`

- **Type:** enum
- **Extracted in call:** disruption
- **Used in scoring by:** cy_R
- **Meaning:** The state of recovery.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `restored_quickly`: Restored in some hours.
  - `restoring_days`: Restoration takes days.
  - `prolonged`: Restoration takes weeks.
  - `unrecovered`: Not recovered, or the systems must be built again.

#### Terrorism/Breach bucket (`terror`, domains D09, D06)

##### `operation_stage`

- **Type:** enum
- **Extracted in call:** actors
- **Used in scoring by:** tb_O
- **Meaning:** The most advanced stage that the hostile operation got to.
- **Grouping-list counterpart:** `EVT_OUTCOME` (partly covered)
- **Allowed labels:**
  - `threat_or_propaganda`: Only a threat, propaganda or a statement.
  - `recruitment_or_surveillance`: Recruitment, a new cell, or surveillance of targets.
  - `procurement_or_training`: The group got weapons or money, or did training.
  - `imminent_preparation`: The last preparation. The attack is about to occur.
  - `attempted_or_intercepted`: The attack was tried, or somebody stopped it during the attack.
  - `executed`: The attack occurred.

##### `weapon_class`

- **Type:** enum
- **Extracted in call:** actors
- **Used in scoring by:** tb_W
- **Meaning:** The most destructive weapon that was used or prepared.
- **Grouping-list counterpart:** `WPN_EXPLOSIVE` (partly covered), `WPN_SMALLARMS` (partly covered), `WPN_DRONE` (partly covered), `WPN_MISSILE` (partly covered), `WPN_HEAVY` (partly covered), `WPN_AIR` (partly covered), `WPN_CBRN` (partly covered)
- **Allowed labels:**
  - `none_or_unknown`: The article gives no weapon.
  - `blade_or_blunt`: A knife, blade or blunt weapon.
  - `firearms`: Firearms.
  - `explosives`: Explosives, an IED or grenades.
  - `vehicle`: A vehicle used as a weapon.
  - `heavy_weapons`: Rockets, mortars, drones with explosives or other heavy weapons.
  - `cbrn`: Chemical, biological, radiological or nuclear weapons.

##### `coordination`

- **Type:** enum
- **Extracted in call:** actors
- **Used in scoring by:** tb_N
- **Meaning:** The organisation that did the operation.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `lone_actor`: One person who acts alone.
  - `small_group`: A small informal group.
  - `organised_cell`: An organised cell or gang.
  - `multi_cell_network`: Two or more cells, or a network.
  - `cross_border_or_multi_site`: A network across borders, or coordinated attacks at two or more sites.

##### `breach_depth`

- **Type:** enum
- **Extracted in call:** actors
- **Used in scoring by:** tb_B
- **Meaning:** The deepest security layer that the attackers or intruders went through.
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `no_breach`: No breach. Only a threat or a report.
  - `approach_or_reconnaissance`: The attackers came near a protected site or did reconnaissance of it.
  - `outer_perimeter`: The attackers went through an outer controlled perimeter or a border.
  - `restricted_area`: The attackers got into a restricted internal area.
  - `protected_asset_or_person`: The attackers got to a protected asset or person.
  - `target_controlled`: The attackers damaged, sabotaged or took control of a high-value target.

##### `facilitation`

- **Type:** enum
- **Extracted in call:** actors
- **Used in scoring by:** tb_F
- **Meaning:** The support network that the article gives (safe houses, couriers, handlers, money, supply of weapons).
- **Grouping-list counterpart:** none (pipeline-only field)
- **Allowed labels:**
  - `none_reported`: The article gives no support element.
  - `single_element`: One support element.
  - `multiple_elements`: Two or more support elements.

### 2.5 Pipeline-only fields (no counterpart in the grouping list)

These stay as they are. They are severity or context fields that the grouping list does not use:

`direction`, `missing`, `service_disruption`, `disruption_duration`, `violence_level`, `institutional_effect`, `coercion_measures`, `dependency`, `replacement_difficulty`, `confidentiality`, `integrity`, `availability`, `subsequent_impact`, `attack_depth`, `recovery_state`, `coordination`, `breach_depth`, `facilitation`.

---

## 3. Part B: unified registry of the grouping-list types (one row per type)

"What the list wants" is the wording of the first row where the type appears. The source rows list several types together, so the wording describes the group (for example `HRM_PROTESTERS` and `HRM_ARRESTED` share "Crowd size, number arrested"). Section 4 shows the wording for each domain.

Each type appears once. "Domains" shows every domain that uses it, with its tier there (A anchor, B supporting, C compatibility only, gate = hard gate). Tiers follow `EVENT_GROUPING_ENTITY_LIST.md`.

### 3.1 Covered: do not add (alias of an extracted field) (8)

| Type | Domains (tier) | What the list wants | Pipeline field today | Gap / note |
|---|---|---|---|---|
| `EVT_DATE` | core:A | Date resolved to an interval with precision (day / week / month / year / unknown). Resolve "Friday", "yesterday" against the publication date. Never fill from the publication date. | `incident_date`, `development_date` | Frame dates. Format YYYY / YYYY-MM / YYYY-MM-DD gives the precision; there is no week precision or interval end. |
| `HRM_PROTESTERS` | D01:B | Crowd size, number arrested | `participants` | Same count, Political bucket only. |
| `HRM_KILLED` | D01:C, D08:C, D09:C | Count with qualifier (exact / at least / approx) and a cumulative flag | `fatalities` | Same count. Pipeline keeps the qualifier but not a cumulative flag. |
| `HRM_WOUNDED` | D01:C, D08:C, D09:C | Count with qualifier (exact / at least / approx) and a cumulative flag | `injuries` | Same count. |
| `HRM_KIDNAPPED` | D01:C, D04:B, D08:C, D09:C | Count with qualifier (exact / at least / approx) and a cumulative flag | `hostages` | Same count (taken as hostages or abducted). |
| `HRM_DAMAGE` | D01:C | Count with qualifier (exact / at least / approx) and a cumulative flag | `asset_damage` | Pipeline gives a severity label, not a count. |
| `HRM_AFFECTED` | D03:C, D04:C, D06:C, D08:C | People affected or displaced | `exposed_count` | Close, but `exposed_count` excludes casualties and allows only five units. |
| `HRM_DISPLACED` | D03:C, D08:C | People affected or displaced | `displaced` | Same count. |

### 3.2 Partly covered: extend or attach to the existing field (32)

| Type | Domains (tier) | What the list wants | Pipeline field today | Gap / note |
|---|---|---|---|---|
| `LOC_POINT` | core:A | City, town, village, landmark of the event. Link to a place id and its parent chain. | `geo_extent`, `affected_places_count` | Pipeline gives how far the effect goes and how many places, not their names or ids. |
| `LOC_ADMIN` | core:A | State, province, district of the event. | `geo_extent`, `affected_places_count` | Same: size of the area only, no named state or district. |
| `LOC_COUNTRY` | core:A | Country of the event, plus origin / target countries as separate roles. | `geo_extent` | `multi_country` / `national` only; no country names or origin/target roles. |
| `ACT_TARGET` | core:A | Who or what the action was done to. Link to a canonical id. | `target_class` | Class of the target only (for example `energy_infrastructure`), not the named target. |
| `EVT_MODE` | core:gate | factual / future / hypothetical / negated / claimed. A threat, plan or claim never merges with a factual event. | `certainty`, `article_kind` | `certainty` (asserted / alleged_or_reported / speculative) and `article_kind` = forthcoming cover most values. `negated` is not captured. |
| `EVT_OUTCOME` | core:B | Status of the event (occurred, foiled, ongoing, resolved). | `trend`, `operation_stage`, `article_kind` | `trend` (resolved, contained, ...) gives the course; `operation_stage` = attempted_or_intercepted gives "foiled", but only in the Terror bucket. |
| `ACT_ARMED_FORCES` | D01:A, D04:B, D05:B, D09:B | Force or unit involved (state, branch, unit) | `factions_count` | Count of groups or security actors, Political bucket only; no force or unit names. |
| `EVT_CLAIM` | D01:A, D09:A | Who claimed or denied responsibility, and the claim text | `attribution`, `certainty` | `claimed_by_party` and `alleged_or_reported` show that a claim exists; who claimed and the claim text are not captured. |
| `WPN_EXPLOSIVE` | D01:B, D09:B | Weapon family used | `weapon_class` | Label `explosives`. Terror bucket only; system or model not captured. |
| `WPN_DRONE` | D01:B, D05:A, D09:B | Weapon family used | `weapon_class` | Label `heavy_weapons` (shared with missile, heavy, air). Terror bucket only. |
| `WPN_MISSILE` | D01:B, D05:A, D09:B | Weapon family used | `weapon_class` | Label `heavy_weapons`. Terror bucket only; D05 wants the system or model. |
| `WPN_SMALLARMS` | D01:B, D09:B | Weapon family used | `weapon_class` | Label `firearms`. Terror bucket only. |
| `WPN_HEAVY` | D01:B, D05:A | Weapon family used | `weapon_class` | Label `heavy_weapons`. Terror bucket only. |
| `WPN_AIR` | D01:B, D05:A | Weapon family used | `weapon_class` | Label `heavy_weapons`. Terror bucket only. |
| `HRM_ARRESTED` | D01:B, D06:B, D09:B | Crowd size, number arrested | `arrests` | Same count, but `arrests` exists only in the Political bucket. D06 and D09 (Terror bucket) do not get it. |
| `INF_PUBLIC` | D01:B, D03:B, D08:B | Soft target type (market, mosque, bus stop) | `target_class` | Class level via `general_public`, `public_gathering`, `religious_site`, ...; no named site. |
| `HRM_CIVILIAN` | D01:C, D08:C, D09:C | Count with qualifier (exact / at least / approx) and a cumulative flag | `fatalities`, `injuries` | Victim sub-group inside killed or wounded; not split out. |
| `HRM_SF_CASUALTY` | D01:C | Count with qualifier (exact / at least / approx) and a cumulative flag | `fatalities`, `injuries` | Security-force sub-group inside killed or wounded; not split out. |
| `EVT_ELECTION` | D02:A | Election, office, constituency | `target_class` | `election_system` marks an attack on an election; the election, office and constituency are not captured. |
| `AMT_PERCENT` | D02:C, D03:C | Vote share, turnout, rate | `capacity_lost_pct`, `price_change_pct`, `recovery_pct` | Only these three percentages. Vote share, turnout and rates are not captured. |
| `INF_ENERGY` | D03:A | Named asset (pipeline, port, crossing, route) + its site | `target_class` | Label `energy_infrastructure`; no named asset. |
| `INF_TRANSPORT` | D03:A | Named asset (pipeline, port, crossing, route) + its site | `target_class` | Label `transport_infrastructure`; no named asset. |
| `INF_DIGITAL` | D03:A | Named asset (pipeline, port, crossing, route) + its site | `target_class` | Label `telecom_or_digital`; no named asset. |
| `INF_FINANCIAL` | D03:A | Named asset (pipeline, port, crossing, route) + its site | `target_class` | Label `financial_system`; no named asset. |
| `INF_SUPPLY_CHAIN` | D03:A | Named asset (pipeline, port, crossing, route) + its site | `target_class` | Closest label is `transport_infrastructure`; no named route. |
| `AMT_MONEY` | D03:C, D04:C, D07:C, D08:C | Deal value, price move | `economic_loss`, `affected_value`, `affected_value_total` | Only loss and affected value. Deal value, ransom, pledge, cargo value are not captured. |
| `WPN_CBRN` | D05:A | System or model | `weapon_class` | Label `cbrn`. Terror bucket only. |
| `INF_MILITARY` | D05:A | Base, facility, test range | `target_class` | Label `military`; no named base or range. |
| `EVT_ESCALATION` | D05:B, D06:B, D09:B | Force or unit, state, escalation signal | `trend` | `trend` = escalating / spreading gives the direction; the signal itself is not captured. |
| `CYB_TARGET` | D07:A | Victim system, sector or organisation | `target_class` | `telecom_or_digital` gives the class; the named victim is not captured. |
| `CYB_DATA` | D07:C | Data volume, ransom or loss | `exposed_count` | `exposed_count` with unit `records` gives the record count; data volume is not captured. `records_compromised` existed in fields v0 to v2. |
| `HRM_NEUTRALISED` | D09:B | Arrests, militants neutralised, forces, victims, linked cyber group | `fatalities` | Militants killed fall inside `fatalities` if the article says killed; "neutralised" is not separate. |

### 3.3 New: add once (55)

| Type | Domains (tier) | What the list wants | Pipeline field today | Gap / note |
|---|---|---|---|---|
| `ACT_INITIATOR` | core:A | Who carried out the action. Link to a canonical id. | – | No field holds the name of the actor. Skills use the hostile actor only to fill other fields. |
| `ACT_MILITANT_ORG` | D01:A, D09:A | Perpetrator group (canonical name + aliases) | – | Perpetrator group name is not extracted. `coordination` only describes its structure (Terror bucket). |
| `ACT_VICTIM_GROUP` | D01:B, D06:B, D08:B, D09:B | Targeted community or group | – | Targeted community is not captured. `target_class` is a category, not a group identity. |
| `WPN_NAVAL` | D01:B, D04:B, D05:A | Weapon family used | – | No `weapon_class` label for naval platforms. |
| `EVT_METHOD` | D01:B, D04:B | Tactic (ambush, shelling, raid) | – | Tactic (ambush, shelling, raid) is not captured. `weapon_class` is the weapon, not the method. |
| `LEG_LAW` | D02:A | Normalised instrument name, parties, date | – | Not captured. |
| `LEG_POLICY` | D02:A | Normalised instrument name, parties, date | – | Not captured. |
| `LEG_TREATY` | D02:A, D05:A, D06:A | Normalised instrument name, parties, date | – | Not captured. |
| `LEG_RESOLUTION` | D02:A, D06:A | Normalised instrument name, parties, date | – | Not captured. |
| `LEG_SANCTION` | D02:A | Normalised instrument name, parties, date | – | Not captured. |
| `NAR_FORUM` | D02:A | Summit, session, venue + date | – | Not captured. |
| `ACT_GOVT` | D02:A, D03:B, D04:B, D05:B, D06:B | Speaker or decision-maker (office + country) | – | Names of speakers and decision-makers are not captured. |
| `ACT_POLITICAL` | D02:B | Party, multilateral body, mediator, dispute forum | – | No party names. |
| `ACT_IGO` | D02:B | Party, multilateral body, mediator, dispute forum | – | No multilateral body names. |
| `ACT_MEDIATOR` | D02:B | Party, multilateral body, mediator, dispute forum | – | No mediator names. |
| `ACT_MECHANISM` | D02:B, D06:A | Party, multilateral body, mediator, dispute forum | – | No dispute forum or commission names. |
| `EVT_DEADLINE` | D02:B, D04:B | Deadline or ultimatum date | – | Deadline or ultimatum date is not captured. `development_date` is the date of the news, not a deadline. |
| `INF_WATER` | D03:A, D06:A, D08:B | Named asset (pipeline, port, crossing, route) + its site | – | No `target_class` label for water systems. |
| `INF_BORDER_CROSS` | D03:A | Named asset (pipeline, port, crossing, route) + its site | – | No `target_class` label for border crossings. |
| `NAM_PROJECT` | D03:A, D08:A | Project, scheme, corridor | – | Not captured. |
| `AMT_TARIFF` | D03:A | Measure, goods covered, parties | – | Not captured. |
| `GOOD_TRADE` | D03:A | Measure, goods covered, parties | – | Goods covered by a measure are not captured. |
| `ACT_CORP` | D03:A, D07:A | Company in the deal or incident | – | No company names. |
| `GOOD_ENERGY` | D03:B | Government actor, commodity, volume | – | Not captured. |
| `AMT_ENERGY` | D03:B | Government actor, commodity, volume | – | Volume not captured. |
| `AMT_WATER` | D03:B, D06:C | Government actor, commodity, volume | – | Flow or level not captured. |
| `GOOD_VESSEL` | D04:A | Vessel name + flag, IMO or hull number, cargo | – | Not captured. |
| `LOC_MARITIME` | D04:A | Sea area, strait, chokepoint, port | – | No place names. `geo_extent` has no maritime value. |
| `EVT_NAMED_OP` | D04:A, D05:A, D09:A | Named operation or exercise | – | Not captured. |
| `STR_NOTICE` | D04:A, D07:B | Navigational warning or advisory id | – | Not captured. |
| `LOC_BORDER` | D04:B, D06:A | Navy or coast guard, flag state, naval platform, maritime border, crew held | – | Not captured. |
| `DEF_PROCUREMENT` | D05:A | Programme, contract, arms deal (parties, system) | – | Not captured. |
| `WPN_AIRDEF` | D05:A | System or model | – | No `weapon_class` label. |
| `WPN_EW_SPACE` | D05:A | System or model | – | No `weapon_class` label. |
| `AMT_DEFENCE_BUDGET` | D05:C | Budget or contract value, units deployed | – | Not captured. The skills say budgets are not `economic_loss`, so keep it separate. |
| `AMT_FORCE_SIZE` | D05:C | Budget or contract value, units deployed | – | Not captured. |
| `LOC_THEATRE` | D06:A | Conflict or counter-terror theatre | – | Not captured. |
| `LOC_WATER` | D06:A | River, basin, dam, with the riparian states | – | Not captured. |
| `CYB_VULN` | D07:A | CVE id and exploitation status | – | Not in fields v3. `cve_ids` existed in fields v0 to v2 and was removed because no formula used it. |
| `CYB_MALWARE` | D07:A | Malware or tool name, campaign or takedown name | – | Not captured. |
| `CYB_CAMPAIGN` | D07:A | Malware or tool name, campaign or takedown name | – | Not captured. |
| `ACT_CYBER_ACTOR` | D07:A, D09:B | Threat group (canonical name + aliases) | – | No threat group names. |
| `CYB_IOC` | D07:A | Indicators and handles | – | Not captured. |
| `SPC_SATELLITE` | D07:A | Satellite or launch object | – | Not captured. |
| `CYB_VECTOR` | D07:B | Attack vector, platform, attributed state, advisory id, wallet | – | Not in fields v3. `mechanism` existed in fields v0 to v2 and was removed. |
| `CYB_PLATFORM` | D07:B | Attack vector, platform, attributed state, advisory id, wallet | – | Not captured. |
| `CYB_ATTRIBUTION` | D07:B | Attack vector, platform, attributed state, advisory id, wallet | – | Attributed state is not captured. `attribution` is the source of facts, a different thing. |
| `AMT_CRYPTO` | D07:B | Attack vector, platform, attributed state, advisory id, wallet | – | Wallet addresses not captured. |
| `EVT_HAZARD` | D08:A | Named storm, quake, outbreak or crisis, with id | – | Named storm, quake or outbreak is not captured. |
| `LOC_DISPLACEMENT_SITE` | D08:A | Camp or displacement site | – | Not captured. |
| `ACT_HUMANITARIAN` | D08:A | Agency or NGO and its response | – | No agency or NGO names. |
| `ACT_CRIMINAL` | D09:A | Perpetrator group or network | – | No criminal network names. |
| `ACT_INDIVIDUAL_RISK` | D09:A | Named suspect or individual of interest | – | No suspect names. |
| `LEG_DESIGNATION` | D09:A | Designation or listing status | – | Not captured. |
| `WPN_SMUGGLED` | D09:B | Weapon or seized cache | – | Seized caches are not a `weapon_class` label. |

---

## 4. Part C: domain by domain, after removing duplicates

For each domain: what to skip, what to attach to an existing field, and what to add. Rows are taken from the grouping list; only the types that need action are shown.

### Core entities (all domains D01–D09)

**Skip (already extracted):** `EVT_DATE` → `incident_date`, `development_date`.

| Type | Tier | What to capture | Status | Attach to / action |
|---|---|---|---|---|
| `LOC_POINT` | A | City, town, village, landmark of the event. Link to a place id and its parent chain. | Partly covered | attach to `geo_extent`, `affected_places_count`: Pipeline gives how far the effect goes and how many places, not their names or ids. |
| `LOC_ADMIN` | A | State, province, district of the event. | Partly covered | attach to `geo_extent`, `affected_places_count`: Same: size of the area only, no named state or district. |
| `LOC_COUNTRY` | A | Country of the event, plus origin / target countries as separate roles. | Partly covered | attach to `geo_extent`: `multi_country` / `national` only; no country names or origin/target roles. |
| `ACT_INITIATOR` | A | Who carried out the action. Link to a canonical id. | New | add as new entry |
| `ACT_TARGET` | A | Who or what the action was done to. Link to a canonical id. | Partly covered | attach to `target_class`: Class of the target only (for example `energy_infrastructure`), not the named target. |
| `EVT_MODE` | gate | factual / future / hypothetical / negated / claimed. A threat, plan or claim never merges with a factual event. | Partly covered | attach to `certainty`, `article_kind`: `certainty` (asserted / alleged_or_reported / speculative) and `article_kind` = forthcoming cover most values. `negated` is not captured. |
| `EVT_OUTCOME` | B | Status of the event (occurred, foiled, ongoing, resolved). | Partly covered | attach to `trend`, `operation_stage`, `article_kind`: `trend` (resolved, contained, ...) gives the course; `operation_stage` = attempted_or_intercepted gives "foiled", but only in the Terror bucket. |

### D01 Conflict, Coercion & Security (Political-Civil bucket)

**Skip (already extracted):** `HRM_PROTESTERS` → `participants`, `HRM_KILLED` → `fatalities`, `HRM_WOUNDED` → `injuries`, `HRM_KIDNAPPED` → `hostages`, `HRM_DAMAGE` → `asset_damage`.

| Type | Tier | What to capture | Status | Attach to / action |
|---|---|---|---|---|
| `ACT_MILITANT_ORG` | A | Perpetrator group (canonical name + aliases) | New | add as new entry |
| `ACT_ARMED_FORCES` | A | Force or unit involved (state, branch, unit) | Partly covered | attach to `factions_count`: Count of groups or security actors, Political bucket only; no force or unit names. |
| `EVT_CLAIM` | A | Who claimed or denied responsibility, and the claim text | Partly covered | attach to `attribution`, `certainty`: `claimed_by_party` and `alleged_or_reported` show that a claim exists; who claimed and the claim text are not captured. |
| `ACT_VICTIM_GROUP` | B | Targeted community or group | New | add as new entry |
| `WPN_EXPLOSIVE` | B | Weapon family used | Partly covered | attach to `weapon_class`: Label `explosives`. Terror bucket only; system or model not captured. |
| `WPN_DRONE` | B | Weapon family used | Partly covered | attach to `weapon_class`: Label `heavy_weapons` (shared with missile, heavy, air). Terror bucket only. |
| `WPN_MISSILE` | B | Weapon family used | Partly covered | attach to `weapon_class`: Label `heavy_weapons`. Terror bucket only; D05 wants the system or model. |
| `WPN_SMALLARMS` | B | Weapon family used | Partly covered | attach to `weapon_class`: Label `firearms`. Terror bucket only. |
| `WPN_HEAVY` | B | Weapon family used | Partly covered | attach to `weapon_class`: Label `heavy_weapons`. Terror bucket only. |
| `WPN_AIR` | B | Weapon family used | Partly covered | attach to `weapon_class`: Label `heavy_weapons`. Terror bucket only. |
| `WPN_NAVAL` | B | Weapon family used | New | add as new entry |
| `HRM_ARRESTED` | B | Crowd size, number arrested | Partly covered | attach to `arrests`: Same count, but `arrests` exists only in the Political bucket. D06 and D09 (Terror bucket) do not get it. |
| `INF_PUBLIC` | B | Soft target type (market, mosque, bus stop) | Partly covered | attach to `target_class`: Class level via `general_public`, `public_gathering`, `religious_site`, ...; no named site. |
| `EVT_METHOD` | B | Tactic (ambush, shelling, raid) | New | add as new entry |
| `HRM_CIVILIAN` | C | Count with qualifier (exact / at least / approx) and a cumulative flag | Partly covered | attach to `fatalities`, `injuries`: Victim sub-group inside killed or wounded; not split out. |
| `HRM_SF_CASUALTY` | C | Count with qualifier (exact / at least / approx) and a cumulative flag | Partly covered | attach to `fatalities`, `injuries`: Security-force sub-group inside killed or wounded; not split out. |

### D02 Political, Diplomatic & Governance (Political-Civil bucket)

| Type | Tier | What to capture | Status | Attach to / action |
|---|---|---|---|---|
| `LEG_LAW` | A | Normalised instrument name, parties, date | New | add as new entry |
| `LEG_POLICY` | A | Normalised instrument name, parties, date | New | add as new entry |
| `LEG_TREATY` | A | Normalised instrument name, parties, date | New | add as new entry |
| `LEG_RESOLUTION` | A | Normalised instrument name, parties, date | New | add as new entry |
| `LEG_SANCTION` | A | Normalised instrument name, parties, date | New | add as new entry |
| `EVT_ELECTION` | A | Election, office, constituency | Partly covered | attach to `target_class`: `election_system` marks an attack on an election; the election, office and constituency are not captured. |
| `NAR_FORUM` | A | Summit, session, venue + date | New | add as new entry |
| `ACT_GOVT` | A | Speaker or decision-maker (office + country) | New | add as new entry |
| `ACT_POLITICAL` | B | Party, multilateral body, mediator, dispute forum | New | add as new entry |
| `ACT_IGO` | B | Party, multilateral body, mediator, dispute forum | New | add as new entry |
| `ACT_MEDIATOR` | B | Party, multilateral body, mediator, dispute forum | New | add as new entry |
| `ACT_MECHANISM` | B | Party, multilateral body, mediator, dispute forum | New | add as new entry |
| `EVT_DEADLINE` | B | Deadline or ultimatum date | New | add as new entry |
| `AMT_PERCENT` | C | Vote share, turnout, rate | Partly covered | attach to `capacity_lost_pct`, `price_change_pct`, `recovery_pct`: Only these three percentages. Vote share, turnout and rates are not captured. |

### D03 Economic, Trade & Infrastructure (Economic bucket)

**Skip (already extracted):** `HRM_AFFECTED` → `exposed_count`, `HRM_DISPLACED` → `displaced`.

| Type | Tier | What to capture | Status | Attach to / action |
|---|---|---|---|---|
| `INF_ENERGY` | A | Named asset (pipeline, port, crossing, route) + its site | Partly covered | attach to `target_class`: Label `energy_infrastructure`; no named asset. |
| `INF_TRANSPORT` | A | Named asset (pipeline, port, crossing, route) + its site | Partly covered | attach to `target_class`: Label `transport_infrastructure`; no named asset. |
| `INF_DIGITAL` | A | Named asset (pipeline, port, crossing, route) + its site | Partly covered | attach to `target_class`: Label `telecom_or_digital`; no named asset. |
| `INF_FINANCIAL` | A | Named asset (pipeline, port, crossing, route) + its site | Partly covered | attach to `target_class`: Label `financial_system`; no named asset. |
| `INF_WATER` | A | Named asset (pipeline, port, crossing, route) + its site | New | add as new entry |
| `INF_BORDER_CROSS` | A | Named asset (pipeline, port, crossing, route) + its site | New | add as new entry |
| `INF_SUPPLY_CHAIN` | A | Named asset (pipeline, port, crossing, route) + its site | Partly covered | attach to `target_class`: Closest label is `transport_infrastructure`; no named route. |
| `NAM_PROJECT` | A | Project, scheme, corridor | New | add as new entry |
| `AMT_TARIFF` | A | Measure, goods covered, parties | New | add as new entry |
| `GOOD_TRADE` | A | Measure, goods covered, parties | New | add as new entry |
| `ACT_CORP` | A | Company in the deal or incident | New | add as new entry |
| `ACT_GOVT` | B | Government actor, commodity, volume | New | add as new entry |
| `GOOD_ENERGY` | B | Government actor, commodity, volume | New | add as new entry |
| `AMT_ENERGY` | B | Government actor, commodity, volume | New | add as new entry |
| `AMT_WATER` | B | Government actor, commodity, volume | New | add as new entry |
| `INF_PUBLIC` | B | Government actor, commodity, volume | Partly covered | attach to `target_class`: Class level via `general_public`, `public_gathering`, `religious_site`, ...; no named site. |
| `AMT_MONEY` | C | Deal value, price move | Partly covered | attach to `economic_loss`, `affected_value`, `affected_value_total`: Only loss and affected value. Deal value, ransom, pledge, cargo value are not captured. |
| `AMT_PERCENT` | C | Deal value, price move | Partly covered | attach to `capacity_lost_pct`, `price_change_pct`, `recovery_pct`: Only these three percentages. Vote share, turnout and rates are not captured. |

### D04 Maritime Security & Operations (Economic bucket)

**Skip (already extracted):** `HRM_KIDNAPPED` → `hostages`, `HRM_AFFECTED` → `exposed_count`.

| Type | Tier | What to capture | Status | Attach to / action |
|---|---|---|---|---|
| `GOOD_VESSEL` | A | Vessel name + flag, IMO or hull number, cargo | New | add as new entry |
| `LOC_MARITIME` | A | Sea area, strait, chokepoint, port | New | add as new entry |
| `EVT_NAMED_OP` | A | Named operation or exercise | New | add as new entry |
| `STR_NOTICE` | A | Navigational warning or advisory id | New | add as new entry |
| `ACT_ARMED_FORCES` | B | Navy or coast guard, flag state, naval platform, maritime border, crew held | Partly covered | attach to `factions_count`: Count of groups or security actors, Political bucket only; no force or unit names. |
| `ACT_GOVT` | B | Navy or coast guard, flag state, naval platform, maritime border, crew held | New | add as new entry |
| `WPN_NAVAL` | B | Navy or coast guard, flag state, naval platform, maritime border, crew held | New | add as new entry |
| `LOC_BORDER` | B | Navy or coast guard, flag state, naval platform, maritime border, crew held | New | add as new entry |
| `EVT_DEADLINE` | B | Navy or coast guard, flag state, naval platform, maritime border, crew held | New | add as new entry |
| `EVT_METHOD` | B | Navy or coast guard, flag state, naval platform, maritime border, crew held | New | add as new entry |
| `AMT_MONEY` | C | Cargo value, crew affected | Partly covered | attach to `economic_loss`, `affected_value`, `affected_value_total`: Only loss and affected value. Deal value, ransom, pledge, cargo value are not captured. |

### D05 Military & Defence (Political-Civil bucket)

| Type | Tier | What to capture | Status | Attach to / action |
|---|---|---|---|---|
| `DEF_PROCUREMENT` | A | Programme, contract, arms deal (parties, system) | New | add as new entry |
| `EVT_NAMED_OP` | A | Named exercise or operation | New | add as new entry |
| `WPN_MISSILE` | A | System or model | Partly covered | attach to `weapon_class`: Label `heavy_weapons`. Terror bucket only; D05 wants the system or model. |
| `WPN_AIR` | A | System or model | Partly covered | attach to `weapon_class`: Label `heavy_weapons`. Terror bucket only. |
| `WPN_AIRDEF` | A | System or model | New | add as new entry |
| `WPN_DRONE` | A | System or model | Partly covered | attach to `weapon_class`: Label `heavy_weapons` (shared with missile, heavy, air). Terror bucket only. |
| `WPN_HEAVY` | A | System or model | Partly covered | attach to `weapon_class`: Label `heavy_weapons`. Terror bucket only. |
| `WPN_NAVAL` | A | System or model | New | add as new entry |
| `WPN_CBRN` | A | System or model | Partly covered | attach to `weapon_class`: Label `cbrn`. Terror bucket only. |
| `WPN_EW_SPACE` | A | System or model | New | add as new entry |
| `INF_MILITARY` | A | Base, facility, test range | Partly covered | attach to `target_class`: Label `military`; no named base or range. |
| `LEG_TREATY` | A | Defence or arms-control agreement | New | add as new entry |
| `ACT_ARMED_FORCES` | B | Force or unit, state, escalation signal | Partly covered | attach to `factions_count`: Count of groups or security actors, Political bucket only; no force or unit names. |
| `ACT_GOVT` | B | Force or unit, state, escalation signal | New | add as new entry |
| `EVT_ESCALATION` | B | Force or unit, state, escalation signal | Partly covered | attach to `trend`: `trend` = escalating / spreading gives the direction; the signal itself is not captured. |
| `AMT_DEFENCE_BUDGET` | C | Budget or contract value, units deployed | New | add as new entry |
| `AMT_FORCE_SIZE` | C | Budget or contract value, units deployed | New | add as new entry |

### D06 Borders, Territory & Transboundary Resources (Terrorism/Breach bucket)

**Skip (already extracted):** `HRM_AFFECTED` → `exposed_count`.

| Type | Tier | What to capture | Status | Attach to / action |
|---|---|---|---|---|
| `LOC_BORDER` | A | Border sector (LoC, IB, LAC segment) | New | add as new entry |
| `LOC_THEATRE` | A | Conflict or counter-terror theatre | New | add as new entry |
| `LOC_WATER` | A | River, basin, dam, with the riparian states | New | add as new entry |
| `INF_WATER` | A | River, basin, dam, with the riparian states | New | add as new entry |
| `LEG_TREATY` | A | Agreement or resolution name | New | add as new entry |
| `LEG_RESOLUTION` | A | Agreement or resolution name | New | add as new entry |
| `ACT_MECHANISM` | A | Joint commission or dispute forum | New | add as new entry |
| `ACT_GOVT` | B | State pair, affected community, border arrests, escalation signal | New | add as new entry |
| `ACT_VICTIM_GROUP` | B | State pair, affected community, border arrests, escalation signal | New | add as new entry |
| `HRM_ARRESTED` | B | State pair, affected community, border arrests, escalation signal | Partly covered | attach to `arrests`: Same count, but `arrests` exists only in the Political bucket. D06 and D09 (Terror bucket) do not get it. |
| `EVT_ESCALATION` | B | State pair, affected community, border arrests, escalation signal | Partly covered | attach to `trend`: `trend` = escalating / spreading gives the direction; the signal itself is not captured. |
| `AMT_WATER` | C | Flow, level, people affected | New | add as new entry |

### D07 Technology, Cyber & Hybrid Security (Cyber bucket)

| Type | Tier | What to capture | Status | Attach to / action |
|---|---|---|---|---|
| `CYB_VULN` | A | CVE id and exploitation status | New | add as new entry |
| `CYB_MALWARE` | A | Malware or tool name, campaign or takedown name | New | add as new entry |
| `CYB_CAMPAIGN` | A | Malware or tool name, campaign or takedown name | New | add as new entry |
| `ACT_CYBER_ACTOR` | A | Threat group (canonical name + aliases) | New | add as new entry |
| `CYB_TARGET` | A | Victim system, sector or organisation | Partly covered | attach to `target_class`: `telecom_or_digital` gives the class; the named victim is not captured. |
| `ACT_CORP` | A | Victim system, sector or organisation | New | add as new entry |
| `CYB_IOC` | A | Indicators and handles | New | add as new entry |
| `SPC_SATELLITE` | A | Satellite or launch object | New | add as new entry |
| `CYB_VECTOR` | B | Attack vector, platform, attributed state, advisory id, wallet | New | add as new entry |
| `CYB_PLATFORM` | B | Attack vector, platform, attributed state, advisory id, wallet | New | add as new entry |
| `CYB_ATTRIBUTION` | B | Attack vector, platform, attributed state, advisory id, wallet | New | add as new entry |
| `STR_NOTICE` | B | Attack vector, platform, attributed state, advisory id, wallet | New | add as new entry |
| `AMT_CRYPTO` | B | Attack vector, platform, attributed state, advisory id, wallet | New | add as new entry |
| `CYB_DATA` | C | Data volume, ransom or loss | Partly covered | attach to `exposed_count`: `exposed_count` with unit `records` gives the record count; data volume is not captured. `records_compromised` existed in fields v0 to v2. |
| `AMT_MONEY` | C | Data volume, ransom or loss | Partly covered | attach to `economic_loss`, `affected_value`, `affected_value_total`: Only loss and affected value. Deal value, ransom, pledge, cargo value are not captured. |

### D08 Humanitarian & Civilian Security (Political-Civil bucket)

**Skip (already extracted):** `HRM_KILLED` → `fatalities`, `HRM_WOUNDED` → `injuries`, `HRM_DISPLACED` → `displaced`, `HRM_KIDNAPPED` → `hostages`, `HRM_AFFECTED` → `exposed_count`.

| Type | Tier | What to capture | Status | Attach to / action |
|---|---|---|---|---|
| `EVT_HAZARD` | A | Named storm, quake, outbreak or crisis, with id | New | add as new entry |
| `LOC_DISPLACEMENT_SITE` | A | Camp or displacement site | New | add as new entry |
| `ACT_HUMANITARIAN` | A | Agency or NGO and its response | New | add as new entry |
| `NAM_PROJECT` | A | Aid programme or appeal plan | New | add as new entry |
| `ACT_VICTIM_GROUP` | B | Affected community, damaged facility | New | add as new entry |
| `INF_WATER` | B | Affected community, damaged facility | New | add as new entry |
| `INF_PUBLIC` | B | Affected community, damaged facility | Partly covered | attach to `target_class`: Class level via `general_public`, `public_gathering`, `religious_site`, ...; no named site. |
| `AMT_MONEY` | C | Appeal or pledge amount | Partly covered | attach to `economic_loss`, `affected_value`, `affected_value_total`: Only loss and affected value. Deal value, ransom, pledge, cargo value are not captured. |
| `HRM_CIVILIAN` | C | Counts with qualifier and cumulative flag | Partly covered | attach to `fatalities`, `injuries`: Victim sub-group inside killed or wounded; not split out. |

### D09 Terrorism, Insurgency & Internal Security (Terrorism/Breach bucket)

**Skip (already extracted):** `HRM_KILLED` → `fatalities`, `HRM_WOUNDED` → `injuries`, `HRM_KIDNAPPED` → `hostages`.

| Type | Tier | What to capture | Status | Attach to / action |
|---|---|---|---|---|
| `ACT_MILITANT_ORG` | A | Perpetrator group or network | New | add as new entry |
| `ACT_CRIMINAL` | A | Perpetrator group or network | New | add as new entry |
| `ACT_INDIVIDUAL_RISK` | A | Named suspect or individual of interest | New | add as new entry |
| `EVT_NAMED_OP` | A | Named operation | New | add as new entry |
| `LEG_DESIGNATION` | A | Designation or listing status | New | add as new entry |
| `EVT_CLAIM` | A | Claim or denial of responsibility | Partly covered | attach to `attribution`, `certainty`: `claimed_by_party` and `alleged_or_reported` show that a claim exists; who claimed and the claim text are not captured. |
| `WPN_EXPLOSIVE` | B | Weapon or seized cache | Partly covered | attach to `weapon_class`: Label `explosives`. Terror bucket only; system or model not captured. |
| `WPN_SMUGGLED` | B | Weapon or seized cache | New | add as new entry |
| `WPN_SMALLARMS` | B | Weapon or seized cache | Partly covered | attach to `weapon_class`: Label `firearms`. Terror bucket only. |
| `WPN_DRONE` | B | Weapon or seized cache | Partly covered | attach to `weapon_class`: Label `heavy_weapons` (shared with missile, heavy, air). Terror bucket only. |
| `WPN_MISSILE` | B | Weapon or seized cache | Partly covered | attach to `weapon_class`: Label `heavy_weapons`. Terror bucket only; D05 wants the system or model. |
| `HRM_ARRESTED` | B | Arrests, militants neutralised, forces, victims, linked cyber group | Partly covered | attach to `arrests`: Same count, but `arrests` exists only in the Political bucket. D06 and D09 (Terror bucket) do not get it. |
| `HRM_NEUTRALISED` | B | Arrests, militants neutralised, forces, victims, linked cyber group | Partly covered | attach to `fatalities`: Militants killed fall inside `fatalities` if the article says killed; "neutralised" is not separate. |
| `ACT_ARMED_FORCES` | B | Arrests, militants neutralised, forces, victims, linked cyber group | Partly covered | attach to `factions_count`: Count of groups or security actors, Political bucket only; no force or unit names. |
| `ACT_VICTIM_GROUP` | B | Arrests, militants neutralised, forces, victims, linked cyber group | New | add as new entry |
| `ACT_CYBER_ACTOR` | B | Arrests, militants neutralised, forces, victims, linked cyber group | New | add as new entry |
| `EVT_ESCALATION` | B | Arrests, militants neutralised, forces, victims, linked cyber group | Partly covered | attach to `trend`: `trend` = escalating / spreading gives the direction; the signal itself is not captured. |
| `HRM_CIVILIAN` | C | Counts with qualifier and cumulative flag | Partly covered | attach to `fatalities`, `injuries`: Victim sub-group inside killed or wounded; not split out. |

---

## 5. Attributes: grouping list versus pipeline

The list asks for these attributes on every entity. Most can be kept on the existing fields without new fields:

| Attribute wanted | Pipeline today | Mapping |
|---|---|---|
| Normalised name | Not stored (values are counts, labels, dates) | Needed only for the **New** and **Partly covered** name types |
| Linked or canonical id | Not stored | Not available; needs a later linking step |
| Role (event place, target place, origin, transit) | Not stored | Not available |
| Evidence quote | Yes: `evidence`, checked verbatim against the article | Reuse as is |
| Source sentence | Not stored separately (the quote is the nearest) | Reuse the quote |
| Confidence | Not stored; `status` and the demote rule act as a coarse check | Not available |
| Count qualifier (exact / at least / approx) | Yes: `qualifier`, plus `at_most` | Reuse; the list's three values are a subset |
| Cumulative-or-incremental flag on counts | Not stored. Cumulative counts are out of scope (`CLAUDE.md`) | **Gap** for every `HRM_*` count |
| Source of the figure | `attribution` and `certainty` give one value for the whole article | Partial |
| Event mode (factual / future / hypothetical / negated / claimed) | `certainty` and `article_kind` | Partial; `negated` missing |

---

## 6. Decisions for the owner (nothing was changed)

1. **Where the new entities would live.** `CLAUDE.md` states that every extracted field must feed a scoring node (fields v3 rule). The domain entities do not feed the score, so they would break that rule if added to `fields_v3.yaml`. Keeping them as a separate identity layer avoids this.
2. **`arrests` for D06 and D09.** `HRM_ARRESTED` is a tier B signal there, but `arrests` is Political-bucket only since fields v3.2.
3. **Cumulative counts.** The list wants a cumulative flag on counts; the pipeline treats cumulative counts as out of scope.
4. **`HRM_AFFECTED` vs `exposed_count`.** Different definitions: `exposed_count` excludes casualties (harness 2.2). Decide whether they are the same field.
5. **Budgets and money.** `AMT_DEFENCE_BUDGET`, `AMT_MONEY` (deal value, pledge, ransom) must not feed `economic_loss`; the skills say budgets and funding are not losses.
6. **Fields removed earlier.** `cve_ids`, `mechanism`, `records_compromised` and `locations` existed in fields v0 to v2. They map to `CYB_VULN`, `CYB_VECTOR`, `CYB_DATA` and the `LOC_*` types. The old definitions could be reused.
7. **Domain proposals are unvalidated.** The grouping list says its domain sets come from 10 articles per domain, and linking accuracy is unmeasured.

---

## 7. Not extracted, and not in the grouping list

The map has 98 types. These 3 are not in the grouping list. The list's own notes name only `EVT_PATTERN` as unused for grouping; `WPN_COMMS` and `WPN_STOCK` are not mentioned there:

`EVT_PATTERN`, `WPN_COMMS`, `WPN_STOCK`.

Also not extracted by the pipeline: names of people, organisations and places; the score values (M, X, D, G, B, S); the hot/historical flag (computed by `newsmetrics/recency.py`); and fields v0 to v2 that fields v3 removed.

