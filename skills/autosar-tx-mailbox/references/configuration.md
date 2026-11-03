# 输入格式与配置依据

## 问题清单

可直接保存用户给出的 Markdown 表格。每条记录必须能确定测试网段和 `id=0x…`，并说明自发周期超规格。多条记录会按网段合并。

也可提供 JSON：

```json
[
  {"network": "BD", "ids": ["0x15E"]},
  {"network": "LC", "id": "0x15E"}
]
```

两种格式均选择问题网段中全部本 ECU 周期 10 ms TX；清单 ID 本身不符合条件时拒绝生成，避免把错误版本或接收报文误配置。问题清单不用于修改报文周期。

## 显式映射清单

用户必须明确提供本次采用的 base ARXML，缺少时先询问；不要自行从工程中选择配置文件。命令行 `--arxml` 和下方 JSON 的 `arxml` 都指用户提供的 base 文件，结果另存为新文件。

discover 生成 JSON，AI 审阅并补齐不确定项。示例使用占位名称，必须用目标工程实际值替换：

```json
{
  "schema_version": 1,
  "project_root": "D:/Project",
  "arxml": "D:/Inputs/baseline.arxml",
  "canif_cfg": "D:/Project/GenData/CanIf_Cfg.h",
  "canif_header": "D:/Project/BSW/CanIf/CanIf.h",
  "networks": [
    {
      "network": "BD",
      "dbc": "D:/Project/Input/BD.dbc",
      "tx_nodes": ["LocalEcu"],
      "controller_path": "/ActiveEcuC/CanIf/CanIfCtrlDrvCfg/BDController",
      "controller_symbol": "CanIfConf_CanIfCtrlCfg_BDController",
      "tx_rule_path": "/ActiveEcuC/BswM/Core0/BswMArbitration/BD_TX"
    }
  ]
}
```

发送节点必须在 DBC 中存在。网络标签不等于 CANoe 通道号。Controller Symbol 必须在指定生成头文件定义，其数值必须与 ARXML 的 CanIfCtrlId 一致。接口声明应位于同一工程源码根目录。DBC 里的枚举默认值也参与发送类型判断；仅支持 Cyclic/Periodic，混合及事件模式不纳入。

当前支持 AUTOSAR R4 命名空间下、单份 ARXML 中的 DaVinci/MICROSAR ECUC Can、CanIf、BswM 结构。定义路径从目标对象沿用，不能用于任意厂家 ECUC 格式的盲写。多版本 DBC、重复 ID 发送 PDU、源配置和 ARXML 控制器 ID 冲突、缺失引用、冲突对象名称均拒绝处理。

## 写入位置

新增对象命名按 E01、T1N 示例保持一致。例如 E0GA 的 BD `0x15E`：Can 硬件对象和 CanIf HTH 均为 `CN_E0X_PT_CarFLZCU_VCU_BD_15E_Tx`，Buffer 为 `CanIfBufferCfg_BD_15E`，BswM 端口为 `BDCanNmTxConfirmationState`。换成其他网段时替换实际网段前缀，例如 `LCCanNmTxConfirmationState`，并使用 LC 的控制器宏和 TX Rule。条件对象沿用 `BswMModeCondition_编号`，编号按本工程现有值续排，不照抄参考工程的固定编号。标准 ID 补足三位，例如 `CanIfBufferCfg_IC_034`。

| 模块 | 对象 | 变化 |
|---|---|---|
| Can | CanHardwareObject | FULL / TRANSMIT / HwSize=1 / HwFifo=false / Multiplexed=false，原控制器，唯一 ObjectId |
| CanIf | CanIfHthCfg | 原控制器，引用新增硬件对象 |
| CanIf | CanIfBufferCfg | NONE / Size=0 / MappedTxPdus=1，引用新增 HTH |
| CanIf | CanIfTxPduCfg | 只改 BufferRef，其他通信属性保留 |
| CanIf | 原 CanIfBufferCfg | 更新剩余发送 PDU 映射数量，保留原 FIFO 容量 |
| CanIf | CanIfPublicCfg | TxConfirmPollingSupport=true |
| BswM | BswMModeRequestPort | 用户条件接口调用，Deferred，初值0 |
| BswM | BswMModeCondition | Equals CANIF_TX_RX_NOTIFICATION |
| BswM | 既有 TX Rule 的表达式 | 原有条件 AND 新确认条件，原动作不变 |

已独占的 FULL 对象在完整引用链检查通过后复用；重复运行不会继续增加邮箱或同名条件。用户可将新输出另存为下一次 manifest 的 arxml，工具会重新检查。

## 参考来源与边界

本方案根据用户明确提供的 E0GA、E01、T1N ARXML 及 E0GA CanIf 源码梳理。E01 示范 FULL 单对象及 NONE/0/1 Buffer；T1N 示范控制器确认查询端口和 TX 表达式条件。源码宏和配置名称始终读取本次目标输入，不随技能分发这些客户工程文件。

XML编辑方法参考 MIT 许可的 davinci-gw 的命名空间解析、ECUC路径和模板克隆；对应许可随包位于 licenses/davinci-gw-MIT.txt。新增原始字节范围编辑、拒绝覆盖和约束校验。没有调用 davinci-gw MCP，也没有把其路由功能或 Python 环境变成本技能的运行依赖。

XML结构校验和语义约束不等于 Vector DaVinci 完整校验。输出由用户手动导入 DaVinci，本技能不执行实际导入验证。节点硬件资源、自动计算参数和目标版本约束留给用户在目标工具中核验，报告明确未执行 DaVinci 导入。
