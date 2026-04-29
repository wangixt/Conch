# StratoVirt 适配变更说明

## 变更文件清单

### 修改文件

| 文件路径 | 修改说明 |
|---------|---------|
| `internal/sandbox/vmm/stratovirt.go` | StratoVirt VMM 客户端实现，主要修改 |

### 新增文档

| 文件路径 | 说明 |
|---------|------|
| `docs/design/stratovirt-adaptation.md` | StratoVirt 适配设计文档 |

## 关键修改点

### 1. 机器类型动态选择 (stratovirt.go:22-32)

```go
func getMachineType() string {
    if runtime.GOARCH == "amd64" || runtime.GOARCH == "x86_64" {
        return "q35"
    }
    return "virt"
}
```

**原因**：microvm 在快照恢复时有内核加载冲突，改用 q35/virt 标准机器类型。

### 2. 启动脚本模板 (stratovirt.go:34-50)

修改点：
- 使用动态机器类型 `{{ .MachineType }}`
- PCI 设备指定总线 `bus=pcie.0,addr=0x10`
- 添加 MachineType 到启动参数结构体

### 3. config.json 生成 (stratovirt.go:463-502)

新增 `generateSnapshotConfig` 方法：
- 在快照创建完成后手动生成 config.json
- 包含 kernel、initrd、memory、vsock 配置

**原因**：StratoVirt migrate 命令不生成 config.json，需要手动生成供恢复使用。

### 4. 快照恢复状态检查 (stratovirt.go:514-552, 344-358)

修改 `LoadSnapshot` 和 `ResumeVM`：
- 发送 cont 命令前检查 VM 状态
- 如果 VM 已经是 Running 状态，跳过 cont 命令

**原因**：StratoVirt 使用 `-incoming file:` 恢复后 VM 已经是 Running 状态，不需要额外的 cont 命令。

## 测试验证

```
✅ Sandbox 创建成功
✅ 快照创建成功
✅ 快照恢复成功
✅ 多次恢复循环测试通过
```

## 使用方法

```python
from conch import Sandbox

# 创建沙箱
sbx = Sandbox.create()

# 创建快照
snapshot = sbx.pause()

# 从快照恢复
sbx2 = Sandbox.create(snapshot_id=snapshot.snapshot_id)
```

## 详细设计文档

完整设计说明请参考：[docs/design/stratovirt-adaptation.md](docs/design/stratovirt-adaptation.md)