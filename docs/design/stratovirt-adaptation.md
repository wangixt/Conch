# Conch StratoVirt 适配设计方案

## 1. 目标与范围

本文档描述 Conch 适配 StratoVirt 虚拟机的设计方案，包括：

- StratoVirt 与 Cloud Hypervisor (CLH) 的架构对比
- 快照机制的差异分析
- 代码修改说明
- 部署和使用指南

## 2. 背景

Conch 最初基于 Cloud Hypervisor (CLH) 设计，支持基于快照的快速沙箱启动。StratoVirt 是华为开源的轻量级虚拟机，具有以下特点：

- 兼容 QEMU 的 QMP 协议
- 支持 microvm 和标准机器类型 (q35/virt)
- 提供快照和迁移功能
- 更好的 openEuler 系统集成

为了扩展 Conch 的虚拟机后端支持，需要适配 StratoVirt，使其能够与 CLH 一样支持沙箱的创建、快照和恢复功能。

## 3. 架构对比

### 3.1 虚拟机管理接口

| 特性 | Cloud Hypervisor (CLH) | StratoVirt |
|------|----------------------|------------|
| **管理协议** | HTTP REST API | QMP (QEMU Machine Protocol) |
| **API端点** | Unix socket + HTTP | Unix socket + JSON |
| **通信方式** | HTTP 请求/响应 | QMP 命令/响应 |
| **状态查询** | `GET /vm-info` | `query-status` |
| **暂停命令** | `PUT /pause` | `stop` |
| **恢复命令** | `PUT /resume` | `cont` |

### 3.2 启动命令对比

**CLH 启动命令示例：**
```bash
cloud-hypervisor \
  --kernel /path/to/vmlinuz \
  --initramfs /path/to/initrd \
  --memory size=1024M \
  --cpus boot=1 \
  --api-socket /path/to/api.sock
```

**StratoVirt 启动命令示例：**
```bash
stratovirt \
  -machine q35 \
  -kernel /path/to/vmlinuz \
  -initrd /path/to/initrd \
  -m 1024M \
  -smp 1 \
  -qmp unix:/path/to/qmp.sock,server,nowait \
  -serial socket,path=/path/to/serial.sock,server,nowait \
  -netdev tap,id=net0,ifname=tap0 \
  -device virtio-net-pci,netdev=net0,id=net0,bus=pcie.0,addr=0x10
```

### 3.3 机器类型选择

StratoVirt 支持多种机器类型：

| 架构 | 推荐机器类型 | 说明 |
|------|-------------|------|
| x86_64 | `q35` | PCIe 总线支持，适合快照恢复 |
| arm64 | `virt` | ARM 标准虚拟机类型 |

**注意：** 原始的 `microvm` 机器类型在快照恢复时存在内核加载冲突问题，因此改用标准机器类型。

### 3.4 PCI 设备配置

使用 q35/virt 机器类型时，PCI 设备需要指定总线：

```bash
# microvm 使用 MMIO 设备
-device virtio-net-device,netdev=net0,id=net0

# q35/virt 使用 PCI 设备，需要指定总线
-device virtio-net-pci,netdev=net0,id=net0,bus=pcie.0,addr=0x10
```

## 4. 快照机制对比

### 4.1 快照创建流程

**CLH 快照创建：**

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Pause VM  │ --> │  Snapshot   │ --> │   Generate  │
│ PUT /pause  │     │ PUT /snapshot│    │  config.json│
└─────────────┘     └─────────────┘     └─────────────┘
```

CLH 的 snapshot API 会：
1. 暂停 VM
2. 导出 VM 状态数据
3. 自动生成 config.json 配置文件
4. 返回完整的快照目录结构

**StratoVirt 快照创建：**

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Pause VM  │ --> │   Migrate   │ --> │   Generate  │
│    stop     │     │ migrate file│     │  config.json│
└─────────────┘     └─────────────┘     │  (手动)     │
                                        └─────────────┘
```

StratoVirt 的 migrate 命令：
1. 仅导出 memory 和 state 文件
2. 不生成 config.json，需要手动生成
3. 快照数据文件：`memory` 和 `state`

### 4.2 快照数据结构

**CLH 快照目录：**
```
snapshot_dir/
├── config.json      # VM 配置信息 (自动生成)
├── memory           # 内存数据
└── state            # 设备状态
```

**StratoVirt 快照目录：**
```
snapshot_dir/
├── config.json      # VM 配置信息 (需手动生成)
├── memory           # 内存数据
└── state            # 设备状态
```

config.json 必须包含以下信息：
```json
{
  "payload": {
    "kernel": "/path/to/vmlinuz",
    "initramfs": "/path/to/initrd"
  },
  "memory": {
    "size": 1073741824,
    "zones": [
      {
        "id": "mem0",
        "size": "1024M",
        "shared": true
      }
    ]
  },
  "vsock": {
    "cid": 42
  }
}
```

### 4.3 快照恢复流程

**CLH 快照恢复：**

```
┌─────────────────┐     ┌─────────────┐     ┌─────────────┐
│  Start Empty VM │ --> │   Restore   │ --> │   Resume    │
│  (no kernel)    │     │ PUT /restore│     │  PUT /resume│
└─────────────────┘     └─────────────┘     └─────────────┘
```

CLH 恢复特点：
1. 启动空 VM（不指定 kernel/initrd）
2. 调用 restore API 加载快照
3. VM 自动进入 Paused 状态
4. 发送 resume 命令恢复运行

**StratoVirt 快照恢复：**

```
┌─────────────────────────┐     ┌─────────────┐
│  Start VM with snapshot │ --> │   Running   │
│  -incoming file:snap    │     │  (自动)     │
└─────────────────────────┘     └─────────────┘
```

StratoVirt 恢复特点：
1. 启动时指定 kernel/initrd 和 `-incoming` 参数
2. VM 启动过程中自动加载快照数据
3. 加载完成后 VM 直接进入 Running 状态
4. **不需要** 发送 cont 命令恢复

### 4.4 关键差异总结

| 差异项 | CLH | StratoVirt |
|-------|-----|------------|
| config.json生成 | 自动 | 手动 |
| 恢复启动方式 | 空VM + restore | 带参数启动 |
| 恢复后状态 | Paused | Running |
| 是否需要cont命令 | 需要 | 不需要 |
| 机器类型限制 | 无 | 需使用q35/virt |

## 5. 代码修改说明

### 5.1 新增文件结构

```
internal/sandbox/vmm/
├── client.go              # VMM 客户端接口
├── cloud_hypervisor.go    # CLH 实现
├── stratovirt.go          # StratoVirt 实现 (修改)
├── process.go             # VMM 进程管理
└── ...
```

### 5.2 stratovirt.go 主要修改

#### 5.2.1 机器类型动态选择

```go
func getMachineType() string {
    if runtime.GOARCH == "amd64" || runtime.GOARCH == "x86_64" {
        return "q35"
    }
    return "virt"
}
```

启动脚本模板修改：
```go
const startScriptStratovirt = `ip netns exec {{ .NamespaceID }} \
{{ .VmmBinaryPath }} \
-machine {{ .MachineType }} \
-kernel {{ .KernelPath }} \
-initrd {{ .RootfsPath }} \
-append "console=ttyS0 reboot=k quiet panic=1 root=/dev/ram0 rw conch.sandbox_id={{ .SandboxId }}" \
-m {{ .MemorySize }}M \
-smp {{ .CPUBoot }} \
-qmp unix:{{ .VmmSocket }},server,nowait \
-serial socket,path={{ .SerialSocket }},server,nowait \
-netdev tap,id=net0,ifname={{ .TapName }} \
-device virtio-net-pci,netdev=net0,id=net0,bus=pcie.0,addr=0x10 \
-disable-seccomp`
```

#### 5.2.2 快照 config.json 生成

新增 `generateSnapshotConfig` 方法：

```go
func (s *StratovirtClient) generateSnapshotConfig(snapfilePath string) error {
    if s.config == nil {
        return fmt.Errorf("snapshot config not available")
    }

    config := map[string]interface{}{
        "payload": map[string]interface{}{
            "kernel":    s.config.kernelPath,
            "initramfs": s.config.initrdPath,
        },
        "memory": map[string]interface{}{
            "size": s.config.memorySize * 1024 * 1024,
            "zones": []map[string]interface{}{
                {
                    "id":     "mem0",
                    "size":   fmt.Sprintf("%dM", s.config.memorySize),
                    "shared": true,
                },
            },
        },
        "vsock": map[string]interface{}{
            "cid": s.config.vsockCID,
        },
    }

    configData, err := json.MarshalIndent(config, "", "  ")
    if err != nil {
        return fmt.Errorf("failed to marshal snapshot config: %w", err)
    }

    configPath := filepath.Join(snapfilePath, "config.json")
    if err := os.WriteFile(configPath, configData, 0640); err != nil {
        return fmt.Errorf("failed to write snapshot config: %w", err)
    }

    return nil
}
```

在 `CreateSnapshot` 中调用：
```go
if status == "completed" {
    logger.Info("Snapshot completed successfully")
    if err := s.generateSnapshotConfig(snapfilePath); err != nil {
        return fmt.Errorf("failed to generate snapshot config: %w", err)
    }
    return nil
}
```

#### 5.2.3 快照恢复状态检查

修改 `LoadSnapshot` 方法，添加状态检查：

```go
func (s *StratovirtClient) LoadSnapshot(snapfilePath string, preferVNC bool) error {
    logger := ulog.GetLogger()
    logger.Info("Loading snapshot (Stratovirt)",
        ulog.F("path", snapfilePath),
    )

    // 关键：检查 VM 状态
    status, err := s.queryStatus()
    if err != nil {
        logger.Warn("Failed to query VM status before resume", ulog.F("error", err))
    } else {
        logger.Debug("VM status before LoadSnapshot", ulog.F("status", status))
        if status == "running" {
            // StratoVirt -incoming 恢复后 VM 已经是 Running 状态
            logger.Info("VM is already running, skip cont command")
            return nil
        }
    }

    // 只有 Paused 状态才发送 cont 命令
    conn, reader, err := s.connectQMP()
    if err != nil {
        return err
    }
    defer conn.Close()

    contCmd := `{"execute": "cont"}\n`
    _, err = conn.Write([]byte(contCmd))
    if err != nil {
        return fmt.Errorf("failed to resume vm: %w", err)
    }

    // ... 处理响应
    return nil
}
```

同样修改 `ResumeVM` 方法：
```go
func (s *StratovirtClient) ResumeVM() error {
    logger := ulog.GetLogger()
    logger.Debug("Resuming VM (Stratovirt)")

    // 检查 VM 状态，避免重复发送 cont 命令
    status, err := s.queryStatus()
    if err != nil {
        logger.Warn("Failed to query VM status before resume", ulog.F("error", err))
    } else {
        logger.Debug("VM status before ResumeVM", ulog.F("status", status))
        if status == "running" {
            logger.Info("VM is already running, skip resume")
            return nil
        }
    }

    return s.executeQMPCommand("cont", nil)
}
```

#### 5.2.4 QMP 命令实现

新增 QMP 协议支持函数：

```go
// 连接 QMP socket
func (s *StratovirtClient) connectQMP() (net.Conn, *bufio.Reader, error)

// 执行 QMP 命令
func (s *StratovirtClient) executeQMPCommand(command string, arguments map[string]interface{}) error

// 查询 VM 状态
func (s *StratovirtClient) queryStatus() (string, error)

// 暂停 VM
func (s *StratovirtClient) PauseVM() error

// 创建快照 (使用 migrate 命令)
func (s *StratovirtClient) CreateSnapshot(snapfilePath string) error

// 加载快照 (状态检查 + cont 命令)
func (s *StratovirtClient) LoadSnapshot(snapfilePath string, preferVNC bool) error
```

### 5.3 恢复脚本模板

```go
const resumeScriptStratovirt = `ip netns exec {{ .NamespaceID }} \
{{ .VmmBinaryPath }} \
-machine {{ .MachineType }} \
-kernel {{ .KernelPath }} \
-initrd {{ .RootfsPath }} \
-append "console=ttyS0 reboot=k quiet panic=1 root=/dev/ram0 rw" \
-m {{ .MemorySize }}M \
-smp {{ .CPUBoot }} \
-qmp unix:{{ .VmmSocket }},server,nowait \
-serial socket,path={{ .SerialSocket }},server,nowait \
-netdev tap,id=net0,ifname={{ .TapName }} \
-device virtio-net-pci,netdev=net0,id=net0,bus=pcie.0,addr=0x10 \
-disable-seccomp \
-incoming file:{{ .SnapfilePath }}`
```

关键点：
1. 仍然指定 kernel 和 initrd（StratoVirt 需要）
2. `-incoming file:` 参数指定快照路径
3. 启动时自动加载快照数据

## 6. 流程图

### 6.1 StratoVirt 沙箱创建流程

```
┌──────────────────┐
│  Sandbox.create  │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│ Prepare Rootfs   │
│   Snapshot       │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Prepare VM      │
│   Snapshot       │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Build Start Cmd │──────► getMachineType() ─► q35/virt
│  (with template) │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Start VMM       │──────► stratovirt -machine q35 ...
│  Process         │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Connect QMP     │
│  & Negotiate     │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Check VM Status │──────► query-status
│  (wait running)  │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Sandbox Ready   │
└──────────────────┘
```

### 6.2 StratoVirt 快照创建流程

```
┌──────────────────┐
│  Sandbox.pause   │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Pause VM        │──────► QMP: stop
│  (QMP command)   │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Wait Paused     │──────► query-status ─► "paused"
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Create Snapshot │──────► QMP: migrate file:path
│  (migrate)       │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Wait Completed  │──────► query-migrate ─► "completed"
└──────────────────┘
         │
         ▼
┌──────────────────┐
│ Generate Config  │──────► Write config.json
│  (manual)        │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Commit Snapshot │──────► containerd snapshot commit
│  to Containerd   │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Stop & Cleanup  │
│  Sandbox         │
└──────────────────┘
```

### 6.3 StratoVirt 快照恢复流程

```
┌──────────────────┐
│ Sandbox.create   │
│ (snapshot_id)    │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│ Acquire Resume   │
│  Workspace       │──────► Mount snapshots
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Build Resume Cmd│──────► -incoming file:snap_path
│  (with template) │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Start VMM       │──────► stratovirt -incoming ...
│  Process         │        (自动加载快照)
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Wait QMP Ready  │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│ LoadSnapshot     │──────► query-status
│                  │        if running: skip cont
│                  │        else: send cont
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  ResumeVM        │──────► query-status
│  (with check)    │        if running: skip
└──────────────────┘
         │
         ▼
┌──────────────────┐
│ Check Daemon     │
│  Alive           │
└──────────────────┘
         │
         ▼
┌──────────────────┐
│  Sandbox Ready   │
└──────────────────┘
```

## 7. 配置说明

### 7.1 VMM 配置

在 `config/config.yaml` 中配置 VMM 类型：

```yaml
vmm:
  type: stratovirt  # 或 cloud-hypervisor
```

### 7.2 StratoVirt 安装要求

```bash
# openEuler 系统安装
yum install stratovirt

# 或从源码编译
git clone https://gitee.com/openeuler/stratovirt
cd stratovirt
cargo build --release
```

### 7.3 Kernel 镜像要求

StratoVirt 需要兼容的 kernel 镜像：
- 支持串口控制台输出 (console=ttyS0)
- 支持内存快照恢复
- 建议使用 openEuler 提供的 kernel

## 8. 使用示例

### 8.1 创建沙箱

```python
from conch import Sandbox

# 创建新沙箱
sbx = Sandbox.create()
print(f'Sandbox: {sbx.sandbox_id}, IP: {sbx.ip}')
```

### 8.2 创建快照

```python
# 暂停沙箱并创建快照
snapshot = sbx.pause()
print(f'Snapshot: {snapshot.snapshot_id}')
```

### 8.3 从快照恢复

```python
# 从快照创建新沙箱
sbx2 = Sandbox.create(snapshot_id=snapshot.snapshot_id)
print(f'Restored: {sbx2.sandbox_id}, IP: {sbx2.ip}')
```

## 9. 测试验证

### 9.1 测试结果

以下测试已在 openEuler 2403 x86_64 系统上验证通过：

| 测试项 | 状态 | 说明 |
|-------|------|------|
| Sandbox 创建 | ✅ 通过 | 使用 q35 机器类型 |
| Sandbox 暂停 | ✅ 通过 | QMP stop 命令 |
| 快照创建 | ✅ 通过 | migrate + config.json生成 |
| 快照恢复 | ✅ 通过 | 状态检查避免重复cont |
| 多次恢复循环 | ✅ 通过 | 3次循环测试成功 |

### 9.2 日志示例

快照恢复成功日志：
```
VM status before LoadSnapshot status=running
VM is already running, skip cont command
VM status before ResumeVM status=running
VM is already running, skip resume
```

## 10. 已知限制

### 10.1 恢复后再次创建快照

当前不支持从恢复后的沙箱再次创建快照。原因：
- 恢复的沙箱没有对应的 containerd active snapshot
- 需要在恢复时创建新的 active snapshot

### 10.2 microvm 机器类型

microvm 机器类型存在快照恢复时的内核加载冲突：
- 错误：`Failed to find matched region, addr 0x100000`
- 解决方案：使用 q35/virt 标准机器类型

## 11. 未来优化方向

1. **恢复后快照支持**：在恢复流程中创建 active snapshot
2. **microvm 支持**：研究 StratoVirt microvm 快照恢复机制
3. **性能优化**：对比 CLH 和 StratoVirt 的启动和恢复性能
4. **vsock 支持**：完善 StratoVirt 的 vsock 设备配置
5. **GPU 支持**：添加 StratoVirt GPU 设备快照支持

## 12. 参考资料

- [StratoVirt 官方仓库](https://gitee.com/openeuler/stratovirt)
- [StratoVirt 文档](https://gitee.com/openeuler/stratovirt/tree/master/docs)
- [Cloud Hypervisor 文档](https://github.com/cloud-hypervisor/cloud-hypervisor)
- [QMP 协议规范](https://wiki.qemu.org/Features/QMP)