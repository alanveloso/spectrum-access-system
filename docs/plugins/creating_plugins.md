# Criar plugins (adapters, data providers, RF, mecanismos)

Extensão em Python só quando o comportamento é novo. Profiles YAML só selecionam mecanismos e capabilities já registrados.

Índice de authoring de profiles: [`docs/profiles/README.md`](../profiles/README.md).

Validar um profile (sem escrever plugin):

```bash
python -m tools.profile_doctor --id cbrs_winnforum
python -m tools.profile_doctor path/to/custom.yaml
```

Custo de authoring (YAML LOC, plugins, core, reuse):

```bash
python -m tools.profile_cost --id cbrs_winnforum
python -m tools.profile_cost path/to/custom.yaml --json
```

Modelo de profile sem código: `profiles/profiles/templates/custom_profile.template.yaml`.

## Quando usar YAML vs plugin

| Necessidade | Caminho |
| --- | --- |
| Novo regime com mecanismos já no catálogo | Profile YAML + dados/testes |
| Novo dispositivo / rede | Device ou Network Adapter (Python + entry point) |
| Novo protocolo de envelope | Protocol Adapter (Python + entry point) |
| Novo dataset (terreno, clutter, entidades, …) | Data Provider (Python + entry point) |
| Novo modelo de path loss no RF Port | RF model plugin (Python + entry point) |
| Novo mecanismo regulatório / de coordenação | `MechanismContract` + entry point `spectrum_access.mechanisms` |
| Coordination Core | Idealmente **zero** mudança; sem `if country` / `if profile` |

Plugins são código **confiável instalado pelo operador**, não scripts enviados por clientes. Compatibilidade de API e capabilities de segurança falham fechado.

Nomes de plugin (e ids de profile builtin) são tokens `[a-z][a-z0-9_]*` — sem `/`, `\\` ou `..`.

O profile **não lista nomes de adapters** como regra principal. Declara capabilities (`geolocation`, `frequency_range`, `max_eirp`, `terrain`, …). Qualquer plugin que satisfaça o contrato pode ser usado.

## Grupos de entry points

Constantes canônicas: `adapters.discovery` (`GROUP_*` adapters/RF/data) e
`primitives.discovery.GROUP_MECHANISMS`.

| Grupo | Contrato | Status |
| --- | --- | --- |
| `spectrum_access.device_adapters` | `ConsumerAdapter` (`kind=device`) | Ativo |
| `spectrum_access.network_adapters` | `ConsumerAdapter` (`kind=network`) | Ativo |
| `spectrum_access.protocol_adapters` | `ProtocolAdapter` | Ativo |
| `spectrum_access.data_providers` | `DataProvider` | Ativo (discovery); registre no `pyproject` do pacote |
| `spectrum_access.rf_models` | `RfPort` | Ativo |
| `spectrum_access.mechanisms` | `MechanismContract` | Ativo (`primitives.discovery.MechanismDiscovery`) |

O target de cada entry point deve ser uma **classe** do contrato ou uma **callable factory sem argumentos** que devolve uma instância. `api_version` deve bater com a constante do pacote (`ADAPTER_API_VERSION` / `PROTOCOL_API_VERSION` / `PROVIDER_API_VERSION` / `RF_API_VERSION`).

Descoberta é **por chamada** (`AdapterDiscovery`, `DataProviderDiscovery`, `RfModelDiscovery`, `MechanismDiscovery`), não um singleton global do core.

Built-ins de referência (este repositório):

```toml
[project.entry-points."spectrum_access.device_adapters"]
mapping = "adapters.device:MappingDeviceAdapter"
cbsd = "adapters.cbsd:cbsd_device_adapter"

[project.entry-points."spectrum_access.network_adapters"]
mapping = "adapters.device:MappingNetworkAdapter"
managed = "adapters.managed_consumer:managed_network_adapter"

[project.entry-points."spectrum_access.protocol_adapters"]
generic_json = "adapters.protocol:GenericJsonProtocolAdapter"
winnforum_rest = "adapters.winnforum_rest:winnforum_rest_protocol_adapter"
elsa1 = "adapters.elsa1:elsa1_protocol_adapter"

[project.entry-points."spectrum_access.rf_models"]
free_space = "rf.external_propagation:free_space_rf_adapter"
itm = "rf.external_propagation:itm_rf_adapter"
```

## RF model (path loss)

Contrato: `rf.port.RfPort` (`path_loss`).

Implementação de referência atual:

```text
DeploymentConfig.rf.plugin
        ↓
RuntimeComposition.rf
        ↓
rf.external_propagation (adapter)
        ↓
spectrum_propagation  (pacote externo)
```

Não importe `reference_models.propagation.wf_itm` nem chame
`CalcItmPropagationLoss` a partir de serviços SAS. ITM concreto vive no
pacote externo (bridge WInnForum opcional).

Rel1Ext / ESC / hybrid / antena permanecem orquestração regulatória no SAS
(ver `docs/architecture/rf.md`).

## Device / Network Adapter

Contrato: `adapters.device.ConsumerAdapter`.

Obrigatório:

- `api_version` → `ADAPTER_API_VERSION` (`"1.0.0"`)
- `kind` → `AdapterKind.DEVICE` ou `AdapterKind.NETWORK`
- `advertised_capabilities()` → frozenset de tokens (ex.: `geolocation`, `frequency_range`, `max_eirp`)
- `to_consumer(payload)` → `ConsumerView` (holder opaco + capabilities + footprints)

Não coloque nomes de vendor no YAML do profile. Publique capabilities; o profile exige capabilities.

Exemplo mínimo (pacote externo):

```python
# my_plugins/device_radio_x.py
from adapters.device import (
    ADAPTER_API_VERSION,
    AdapterKind,
    ConsumerView,
    DEVICE_CAPABILITY_FREQUENCY_RANGE,
    DEVICE_CAPABILITY_GEOLOCATION,
    DEVICE_CAPABILITY_MAX_EIRP,
)

class RadioXDeviceAdapter:
    api_version = ADAPTER_API_VERSION
    kind = AdapterKind.DEVICE

    def advertised_capabilities(self):
        return frozenset(
            {
                DEVICE_CAPABILITY_GEOLOCATION,
                DEVICE_CAPABILITY_FREQUENCY_RANGE,
                DEVICE_CAPABILITY_MAX_EIRP,
            }
        )

    def to_consumer(self, payload):
        # mapear payload externo → ConsumerView canônico
        ...
```

```toml
[project.entry-points."spectrum_access.device_adapters"]
radio_x = "my_plugins.device_radio_x:RadioXDeviceAdapter"
```

Referência in-tree: `adapters.device.MappingDeviceAdapter`, `adapters.cbsd`.

## Protocol Adapter

Contrato: `adapters.protocol.ProtocolAdapter`.

- `api_version` → `PROTOCOL_API_VERSION` (igual a `ADAPTER_API_VERSION`)
- `protocol_id` → token estável do protocolo
- `decode(envelope, consumer_adapter)` → `ProtocolInbound` (`DomainOperation` + `SpectrumRequest`)
- `encode_decision(decision)` → envelope de resposta

Identidade física do rádio permanece no `ConsumerAdapter`. O protocolo só traduz o envelope.

Referência: `adapters.protocol.GenericJsonProtocolAdapter`, `adapters.winnforum_rest`.

## Data Provider

Contrato: `providers.contract.DataProvider`.

- `api_version` → `PROVIDER_API_VERSION`
- `provider_id` → id estável
- `advertised_capabilities()` ⊆ `DATA_CAPABILITIES`  
  (`terrain`, `land_cover`, `protected_entities`, `rights`, `boundaries`, `reference_data`)
- Métodos do kind anunciado; `DatasetProvenance` em leituras relevantes

```toml
[project.entry-points."spectrum_access.data_providers"]
campus_terrain = "my_plugins.campus_terrain:CampusTerrainProvider"
```

Profiles v2 listam capabilities em `data.required_capabilities` (e device/network em
`requirements.*_capabilities`), não nomes de vendor. O doctor (`--check-data`) verifica
se algum provider instalado cobre o requisito.

Referência: `providers.contract.MappingTerrainProvider` e demais mapping providers.

## RF model

Contrato: `rf.port.RfPort`.

- `api_version` → `RF_API_VERSION`
- `model_id` → hoje o port tipado é path loss (`RF_MODEL_PATH_LOSS` / `"path_loss"`)
- `path_loss(PathLossRequest)` → `PathLossResult` com `provenance`
- Backend obrigatório ausente → `RfUnavailableError` (fail closed)

A implementação de referência usa `rf.external_propagation`, que adapta o pacote
externo `spectrum_propagation`. Não reimplemente ITM no plugin SAS e **não**
importe `wf_itm` / `CalcItmPropagationLoss` em serviços. Rel1Ext (P.2108 +
activity) e ESC permanecem orquestração regulatória no SAS.

```toml
[project.entry-points."spectrum_access.rf_models"]
my_fspl = "my_plugins.rf_fspl:MyFreeSpaceAdapter"
```

No profile, o mecanismo RF de modelo registrado é `path_loss` (slot `rf_model`).
Seleção de implementação (`free_space`, `itm`, …) é `DeploymentConfig`, não profile.

## Mecanismos (catálogo semântico, não DSL)

Profiles citam `mechanism:` ids presentes no catálogo composto
(`discovered_mechanism_registry()` = seed built-in + entry points).

Para comportamento **novo** (eixo/semântica que ainda não existe):

1. Defina um `MechanismContract` (`mechanism_id`, `axis`, `version`, `required_capabilities`, `slot` se RF).
2. Publique-o no grupo `spectrum_access.mechanisms` (factory zero-arg → `MechanismContract`).
   Built-ins seedam `primitives.registry`; externos **não** precisam editar esse módulo.
3. Implemente a semântica em código de primitive/serviço — **não** em expressões arbitrárias no YAML.
4. Só então referencie o id no Spectrum Profile.

YAML não é linguagem de programação. Sem `if`/`else`, sem fórmulas livres, sem “mini-DSL”.

Discovery só torna o mecanismo **conhecido**; não cria um `execute()` universal sobre IAP/DPA/ESC.
Ver `docs/architecture/extension_boundaries.md`.

## Checklist do autor de plugin

1. Confirme que o comportamento **não** existe já como mecanismo/capability registrada (senão: só YAML).
2. Implemente o Protocol/classe com a `api_version` correta.
3. Anuncie **capabilities**, não nomes de produto no profile.
4. Publique o entry point no `pyproject.toml` (ou equivalente) do pacote instalável.
5. Reinstale o pacote no ambiente (`pip install -e .`).
6. Rode o profile doctor; para dados, `--check-data` / `--require-data-plugins` conforme o caso.
7. Não altere Coordination Core só para plugar um adapter.

## O que este guia não faz

- Não autoriza branching por país/profile no core.
- Não substitui evidência WInnForum / gates oficiais.
- Métricas de custo de profile (core vs plugin vs YAML): `python -m tools.profile_cost` .
