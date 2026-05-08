# CLH快照恢复的机制分析

## 关键发现：CLH恢复流程完全不同

### CLH恢复脚本

```bash
# resumeScriptCLH (cloud_hypervisor.go:40-43)
ip netns exec {{ .NamespaceID }} \
{{ .VmmBinaryPath }} --api-socket \
{{ .VmmSocket }} \
--seccomp false
```

**关键特点**：
- CLH恢复启动时**不带任何设备配置**！
- 不指定kernel、initrd、pmem、memory等
- 只启动一个空的VM进程，监听API socket
- 然后通过HTTP API恢复快照

### CLH快照恢复流程

```go
// cloud_hypervisor.go:259
func (c *CLHClient) LoadSnapshot(snapfilePath string, preferVNC bool) error {
    // 1. 通过HTTP API恢复快照
    // 2. CLH从快照文件读取所有配置（包括pmem）
    // 3. CLH内部打开pmem文件，不需要external指定
}
```

**核心差异**：
- CLH启动时不需要指定pmem文件路径
- CLH从快照文件中读取pmem配置
- CLH内部处理pmem文件的打开

### StratoVirt恢复脚本

```bash
# resumeScriptStratovirt (stratovirt.go:47-63)
ip netns exec {{ .NamespaceID }} \
{{ .VmmBinaryPath }} \
-machine q35 \
-kernel {{ .KernelPath }} \
-initrd {{ .RootfsPath }} \
-object memory-backend-file,size=1988M,id=pmem0,mem-path={{ .PmemPath },share=on \
-device virtio-pmem-pci,id=pmem0pci,memdev=pmem0 \
-incoming file:{{ .SnapfilePath }}
```

**关键特点**：
- StratoVirt启动时**必须指定所有设备配置**
- pmem文件路径必须在命令行中指定
- incoming模式启动时就要打开pmem文件
- pmem文件必须可读写

## View Snapshot的可写层问题

### containerd View API

```go
// containerd snapshotter API
func (s *snapshotter) View(
    ctx context.Context,
    namespace, key, parent string,
    opts ...Opt,
) ([]mount.Mount, error) {
    // View创建一个只读snapshot view
    // 没有upperdir（可写层）
    // 只有lowerdir（只读层）
}
```

**View snapshot特性**：
- View是只读snapshot
- overlayfs mount选项：ro（只读）
- 没有upperdir和workdir
- 无法修改内容

### Active Snapshot vs View Snapshot

**Active snapshot（正常启动）**：
```
overlay on /run/conch/snapshot/default/sandbox_xxx/rootfs type overlay (rw,...)
lowerdir=/var/lib/containerd/.../snapshots/17/fs
upperdir=/var/lib/containerd/.../snapshots/576/fs  ← 可写层
workdir=/var/lib/containerd/.../snapshots/576/work
```

**View snapshot（快照恢复）**：
```
overlay on /run/conch/snapshot/default/shared/sha256065a... type overlay (ro,...)
lowerdir=/var/lib/containerd/.../snapshots/xxx/fs  ← 只有只读层
# 没有upperdir！
```

## 为什么CLH可以工作？

### CLH的pmem文件打开时机

**正常启动**：
```
CLH启动命令（带pmem配置）
  ↓
CLH打开pmem文件（overlayfs rw mount）
  ↓
成功 ✅
```

**快照恢复**：
```
CLH启动命令（空VM，不带pmem配置）
  ↓
VM监听API socket
  ↓
HTTP API: vm.restore(snapshot_file)
  ↓
CLH从快照读取配置
  ↓
CLH加载快照数据到内存
  ↓
CLH可能在内部重新配置pmem
  ↓
CLH内部处理，不需要外部指定文件路径
  ↓
成功 ✅
```

### CLH的优势

**不依赖命令行指定的文件路径**：
- 快照恢复时，pmem配置从快照文件中读取
- CLH内部处理文件打开
- 可能不要求文件可写（或内部处理）

**API恢复机制**：
- VM先启动起来（空VM）
- 然后通过API恢复快照
- 可以在正常模式下处理pmem
- 不受incoming模式的限制

## 为什么StratoVirt不行？

### StratoVirt的pmem文件打开时机

**正常启动**：
```
StratoVirt启动命令（带pmem配置）
  ↓
解析命令行参数
  ↓
创建pmem设备 → 打开pmem文件（overlayfs rw mount）
  ↓
成功 ✅
```

**快照恢复**：
```
StratoVirt启动命令（带pmem配置 + incoming）
  ↓
解析命令行参数
  ↓
进入incoming模式
  ↓
创建pmem设备 → 打开pmem文件（overlayfs ro mount）
  ↓
失败 ❌：Read-only file system
```

### StratoVirt的限制

**必须通过命令行指定pmem文件**：
- incoming模式要求设备配置完整
- pmem文件路径必须在启动时指定
- 启动时就要打开文件（OpenOptions::new().write(true))
- overlayfs只读mount无法打开

**incoming机制的限制**：
- 继承QEMU的incoming设计
- 启动时必须完整配置
- 无法像CLH那样延迟加载

## 总结

### View Snapshot没有可写层

**确认**：
- View snapshot确实没有可写层（upperdir）
- overlayfs mount是只读的（ro）
- 这是containerd View API的设计

### CLH和StratoVirt的核心差异

| 项目 | CLH | StratoVirt |
|------|-----|------------|
| 恢复机制 | API恢复（HTTP） | incoming文件恢复 |
| 启动配置 | 空VM + API加载 | 完整配置 + incoming |
| pmem指定时机 | API恢复时 | 启动命令行 |
| pmem打开时机 | 正常模式（API） | incoming模式（启动） |
| overlayfs要求 | 可只读 | 必须读写 |

### CLH可以工作的原因

**关键**：CLH不在启动时打开pmem文件
- 启动空VM（不带pmem配置）
- 通过API恢复快照
- pmem处理在API内部
- 不依赖overlayfs的读写权限

### StratoVirt失败的原因

**关键**：StratoVirt在启动时必须打开pmem文件
- 启动时带完整pmem配置
- 进入incoming模式
- 启动时打开pmem文件（要求write权限）
- View snapshot是只读overlayfs → 失败
