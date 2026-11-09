# Canonical artifact mappings

`traceability.py` derives trace links from the canonical `ProjectSnapshot`; it
does not maintain a second artifact or source registry. The mappings below are
the controlled kind order from standard 4.0. A link is emitted only when both
endpoint kinds are present, so an early capability is not forced to invent a
future release artifact.

```text
source
  -> product_vision -> product_goals, system_scope, requirements_analysis
  -> requirements_analysis -> domain_model, business_requirements
  -> business_requirements -> business_scenario_catalog, functional_requirements,
                              non_functional_requirements
  -> functional_requirements -> acceptance_criteria, c4_context, sdd_specification
  -> c4_context -> c4_container -> c4_component -> c4_dynamic
  -> architecture_baseline -> sdd_specification -> test_report
  -> test_report -> deployment_package -> release_record -> operations_document
```

The implementation treats the standard order as a partial order: when a
requested chain names kinds that are present, an edge exists between each
successive present kind. Missing kinds are reported by
`validate_traceability`, rather than silently bypassed.
