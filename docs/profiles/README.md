# Profile authoring

Documentação de produto para **reference profiles**, **custom profiles** e **plugins**.

| Doc | Conteúdo |
| --- | --- |
| [architecture_overview.md](architecture_overview.md) | Core vs profile vs plugins; o que não vai no YAML |
| [reference_and_custom.md](reference_and_custom.md) | Authoring reference/custom; doctor; claims |
| [../plugins/creating_plugins.md](../plugins/creating_plugins.md) | Adapters, providers, RF, mecanismos |

## Comandos rápidos

```bash
python -m tools.profile_doctor --id cbrs_winnforum
python -m tools.profile_doctor path/to/custom.yaml
python -m tools.profile_cost --id cbrs_winnforum
```

Template: `profiles/profiles/templates/custom_profile.template.yaml`
Exemplo: `profiles/profiles/examples/custom_campus_6ghz.yaml`
