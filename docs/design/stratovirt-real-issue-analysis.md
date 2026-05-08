# StratoVirt pmem文件打开的真正问题

## 关键发现：StratoVirt要求读写权限

### 源码分析（machine_config.rs:226-232）

```rust
let file = std::fs::OpenOptions::new()
    .read(true)
    .write(true)      // ← 关键：要求写权限！
    .create(true)
    .truncate(false)
    .open(path)
    .with_context(|| format!("Failed to open file: {}", path_str))?;
```

**重要发现**：
- StratoVirt使用`OpenOptions::new().write(true).open(path)`
- 这要求文件必须以读写模式打开
- 即使pmem设备主要用于读取，StratoVirt仍然要求写权限

## 为什么overlayfs文件无法打开？

### overlayfs的特性

**overlayfs工作原理**：
```
overlayfs = lowerdir + upperdir + workdir
- lowerdir: 只读层（镜像文件）
- upperdir: 可写层（修改内容）
- workdir: 工作目录

文件访问：
- 读文件：可能从lowerdir读取
- 写文件：触发copy-up机制，文件被复制到upperdir
```

**overlayfs的限制**：

#### 1. 文件权限与实际可写性不一致

**overlayfs mount（View snapshot）**：
```
overlay on /run/conch/snapshot/default/shared/sha256065a... type overlay (ro,...)
lowerdir=/var/lib/containerd/.../snapshots/xxx/fs
# 注意：mount本身是ro（只读）
```

**文件本身**：
```
ls -la /run/conch/snapshot/default/shared/sha256065a.../layer0.erofs
-rw-r--r-- 1 root root 2084569088 Apr 14 21:53 layer0.erofs
# 文件权限看起来是rw，可以读写
```

**矛盾点**：
- 文件权限显示可读写（rw-r--r--）
- 但overlayfs mount是只读的（ro）
- 实际打开时：open()失败，返回"Read-only file system"错误

#### 2. overlayfs只读mount的影响

**只读overlayfs mount**：
```c
open("/overlayfs/path", O_RDWR)
  → overlayfs driver检查mount选项
  → 发现mount是ro（只读）
  → 拒绝写权限请求
  → 返回错误: EROFS (Read-only file system, errno 30)
```

**即使文件权限显示可写，但mount是只读，无法以写模式打开！**

## 正常启动 vs 快照恢复：关键差异

### 正常启动（成功）

**overlayfs mount（Active snapshot）**：
```
overlay on /run/conch/snapshot/default/sandbox_xxx/rootfs type overlay (rw,...)
# mount是rw（读写） ✅
```

**文件打开**：
```rust
OpenOptions::new().write(true).open(path)
  → overlayfs检查mount：是rw
  → 允许写权限 ✅
  → 成功打开文件
```

### 快照恢复启动（失败）

**overlayfs mount（View snapshot）**：
```
overlay on /run/conch/snapshot/default/shared/sha256065a... type overlay (ro,...)
# mount是ro（只读） ❌
```

**文件打开**：
```rust
OpenOptions::new().write(true).open(path)
  → overlayfs检查mount：是ro
  → 拒绝写权限 ❌
  → 错误: Read-only file system (errno 30)
```

## 根本原因总结

**不是文件打开方式的差异**：
- StratoVirt在正常启动和快照恢复时，**使用相同的代码打开文件**
- 都使用`OpenOptions::new().write(true).open(path)`
- 都要求读写权限

**关键差异在于overlayfs mount选项**：
- 正常启动：Active snapshot → overlayfs mount是rw → 可以以写模式打开 ✅
- 快照恢复：View snapshot → overlayfs mount是ro → 无法以写模式打开 ❌

## 为什么StratoVirt需要写权限？

### 可能的原因

#### 1. pmem设备实现需要

```rust
// virtio/src/device/pmem.rs:312-320
let host_addr = match &self.backend.backend {
    Some(file) => do_mmap(
        &Some(file.as_ref()),
        self.backend.size,
        0,
        false,
        self.backend.share,    // ← share=true
        false,
    )?,
    None => bail!("No file opened for virtio-pmem@{}", self.id),
};
```

**mmap可能需要写权限**：
- `MAP_SHARED`映射可能需要文件可写
- share=true意味着内存区域可共享
- 可能需要写权限来支持某些操作

#### 2. 设备配置和维护

StratoVirt可能需要：
- 修改文件大小（`set_len`）
- 维护设备状态
- 支持某些高级特性

#### 3. 兼容性考虑

继承QEMU的设计：
- QEMU的memory-backend-file也要求读写权限
- 为了兼容性和一致性

## 解决方案

### 方案1：修改overlayfs mount为读写

**修改位置**: Conch的snapshot view创建逻辑

**修改方式**：
```go
// 创建View snapshot时，使用读写mount而非只读
opts := []snapshots.Opt{
    snapshots.WithLabels(map[string]string{
        "mount-read-only": "false",  // ← 修改为读写
    }),
}
```

**风险**：
- View snapshot可能被修改（不应该）
- 破坏了View snapshot的只读语义
- 多个VM共享同一个View可能互相影响

### 方案2：修改StratoVirt源码（不推荐）

**修改位置**: machine_manager/src/config/machine_config.rs

**修改方式**：
```rust
// 只使用读权限
let file = std::fs::OpenOptions::new()
    .read(true)
    .write(false)   // ← 修改为false
    .open(path)
    .with_context(|| format!("Failed to open file: {}", path_str))?;
```

**问题**：
- 可能破坏pmem设备功能
- share=true可能需要写权限
- 不符合StratoVirt/QEMU设计

### 方案3：使用ext4而非overlayfs（推荐）

**为StratoVirt创建专用snapshotter**：
- 使用ext4文件而非overlayfs
- View snapshot也以读写mount
- 满足StratoVirt要求

### 方案4：使用CLH（立即可用）

**CLH的优势**：
- CLH不要求pmem文件可写
- 支持overlayfs只读mount
- 快照恢复功能完整

## 最终结论

**真正的问题**：
- StratoVirt要求pmem文件以读写模式打开（`write(true)`）
- View snapshot的overlayfs mount是只读的（ro）
- 只读mount上的文件无法以写模式打开
- 报错："Read-only file system (errno 30)"

**不是**：
- 不是incoming模式的特殊要求
- 不是O_DIRECT或MAP_SYNC的限制
- 不是StratoVirt启动方式的差异

**而是**：
- overlayfs mount选项的差异（rw vs ro）
- StratoVirt对写权限的要求
- View snapshot的只读特性与StratoVirt要求的冲突
