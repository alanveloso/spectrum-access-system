# Reference e custom profiles — authoring

Mesmo schema Spectrum Profile. Diferença: **proveniência / status**, não poder de expressão.

## Status

| `metadata.status` | Significado |
| --- | --- |
| `reference` | Profile mantido pelo projeto com rastreabilidade/testes |
| `custom` | Derivado/experimental do operador |

`status: reference` **não** é certificação regulatória automática. Um custom
`based_on: cbrs_winnforum` **não** herda alegações de conformidade.

## Fluxo sem Python novo

1. Copie o template: `profiles/profiles/templates/custom_profile.template.yaml`
2. Ajuste `metadata.id` (token `[a-z][a-z0-9_]*`), `version`, `status: custom`
3. Edite `spectrum` (faixas/canalização), `access` (cardinalidade livre de classes),
   power/geo/temporal/protection conforme mechanisms **já registrados**
4. Declare capabilities em `requirements` / `data.required_capabilities` — não nomes de adapter
5. Valide:

```bash
python -m tools.profile_doctor path/to/your_profile.yaml
python -m tools.profile_cost path/to/your_profile.yaml --json
```

Exemplo pronto: `profiles/profiles/examples/custom_campus_6ghz.yaml`.

Builtin ids (`cbrs_winnforum`, …) carregam via:

```bash
python -m tools.profile_doctor --id cbrs_winnforum
```

O doctor emite finding `trust/provenance` (hash, status, `based_on`, trust tier).

## Checklist do author de profile

- [ ] Só mechanisms presentes em `discovered_mechanism_registry()` (seed + entry points)
- [ ] Sem DSL / condicionais no YAML
- [ ] Banda no próprio arquivo (sem BandProfile externo)
- [ ] Access classes: zero ou N — ids opacos, não enums do core
- [ ] Capabilities, não vendor names
- [ ] `based_on` só como provenance
- [ ] Doctor PASS no path explícito ou `--id` builtin
- [ ] Se faltar mechanism/capability → **pare** e veja o guia de plugins / catálogo;
      não force `if` no Coordination Core

## Quando parar e abrir plugin / primitive

| Sintoma | Ação |
| --- | --- |
| Precisa de comportamento já coberto por mechanism | Continue no YAML |
| Novo rádio / rede / protocolo / dataset / modelo RF | Plugin (ver `docs/plugins/`) |
| Nova regra regulatória sem mechanism | Primitive + registry (task autorizada); depois YAML |
| “Só um `if brasil` no core” | **Rejeitado** pela arquitetura |

## Claims permitidos na documentação de profile

- “Carrega / doctor PASS / reuse 100% dos mechanisms citados”
- “Holdout TVWS é CONDITIONAL até `query_assignment`” (G10)

Claims **não** permitidos só por existir YAML:

- Certificação FCC / Anatel / Ofcom / ETSI / WInnForum formal
- `PASS_OFFICIAL` sem evidência de `/run-winnforum-gate`

## Leitura adicional

- [architecture_overview.md](architecture_overview.md)
- [creating_plugins.md](../plugins/creating_plugins.md)
