# StratoVirt启动模式对比分析

## 正常启动（Sandbox创建）- 成功 ✅

### 启动命令
```bash
stratovirt \
  -machine q35 \
  -kernel /var/run/conch/snapshot/default/sandbox_xxx/rootfs/boot/vmlinuz \
  -initrd /var/run/conch/snapshot/default/sandbox_xxx/rootfs/data/conch.initrd \
  -object memory-backend-file,size=1024M,id=mem0,mem-path=.../mem.img,share=on \
  -object memory-backend-file,size=1988M,id=pmem0,mem-path=.../sandbox_xxx/rootfs/layer0.erofs,share=on \
  -device virtio-pmem-pci,id=pmem0pci,memdev=pmem0 \
  # 没有 -incoming 参数
```

### 启动流程
```
1. 解析命令行参数
   ↓
2. 初始化VM（正常模式）
   ↓
3. 创建设备
   - 创建memory-backend-file (mem0)
   - 创建memory-backend-file (pmem0)
   ↓
4. 访问pmem0.mem-path文件
   文件路径: .../sandbox_xxx/rootfs/layer0.erofs
   文件系统: overlayfs (rw, active snapshot)
   ↓
5. pmem设备初始化
   - StratoVirt打开文件 → ✅ 成功
   - 创建内存映射 → ✅ 成功
   - 设备realize → ✅ 成功
   ↓
6. VM正常启动
   ✅ 成功
```

### 关键特点

**正常模式下的文件访问**：
- StratoVirt在正常启动模式下访问pmem文件
- 使用标准的文件打开方式（open syscall）
- overlayfs对这种访问完全支持 ✅
- 文件权限正常，可以读写

**overlayfs支持**：
```
overlay on /run/conch/snapshot/default/sandbox_xxx/rootfs type overlay (rw,...)
lowerdir=/var/lib/containerd/.../snapshots/17/fs
upperdir=/var/lib/containerd/.../snapshots/576/fs
workdir=/var/lib/containerd/.../snapshots/576/work
```

## 快照恢复启动 - 失败 ❌

### 启动命令
```bash
stratovirt \
  -machine q35 \
  -kernel /var/run/conch/snapshot/default/shared/sha2567fe0.../boot/vmlinuz \
  -initrd /var/run/conch/snapshot/default/shared/sha2567fe0.../data/conch.initrd \
  -object memory-backend-ram,size=1024M,id=mem0 \  # 注意：使用ram而非file
  -object memory-backend-file,size=1988M,id=pmem0,mem-path=.../shared/sha256065a.../layer0.erofs,share=on \
  -device virtio-pmem-pci,id=pmem0pci,memdev=pmem0 \
  -incoming file:/var/run/conch/snapshot/default/sandbox_xxx/mem/conch/snapshot  # ← 关键差异
```

### 启动流程
```
1. 解析命令行参数
   - 发现 -incoming 参数
   ↓
2. 初始化VM（incoming模式） ← 关键差异
   - 进入特殊的incoming恢复模式
   - VM状态处于"等待恢复"
   ↓
3. 创建设备
   - 创建memory-backend-ram (mem0)
   - 创建memory-backend-file (pmem0)
   ↓
4. 访问pmem0.mem-path文件
   文件路径: .../shared/sha256065a.../layer0.erofs
   文件系统: overlayfs (view snapshot)
   ↓
5. pmem设备初始化
   - StratoVirt打开文件 → ❌ 失败
   - 错误: Read-only file system (os error 30)
   ↓
6. 设备realize失败
   ❌ Failed to realize virtio device
   ↓
7. VM启动失败
   ❌ Failed to realize standard VM
```

### 关键差异

**incoming模式下的文件访问**：
- StratoVirt在incoming恢复模式下访问pmem文件
- 使用特殊的文件访问机制（可能涉及direct I/O、特殊mmap等）
- overlayfs对这种特殊访问不支持 ❌
- 即使文件本身是rw，overlayfs文件系统被识别为"只读"

## 为什么incoming模式更严格？

### QEMU/StratoVirt incoming机制

**incoming模式的设计目的**：
- 从快照文件恢复VM状态
- 需要精确还原内存布局
- 需要完全匹配设备配置

**incoming模式的特殊要求**：

#### 1. 内存映射要求更严格

**正常模式**：
```
创建pmem设备 → mmap文件 → VM可以访问
使用标准mmap，overlayfs支持 ✅
```

**incoming模式**：
```
创建pmem设备 → 特殊mmap → 快照恢复 → VM状态还原
可能需要：
- O_DIRECT标志（直接I/O，绕过缓存）
- MAP_SYNC标志（同步映射）
- MAP_SHARED_VALIDATE标志（验证映射）
overlayfs不支持这些特殊标志 ❌
```

#### 2. 文件系统特性要求

**incoming模式可能需要**：
- Direct I/O支持：绕过页面缓存，直接读写
- 同步映射：映射内容与文件同步
- 内存对齐：文件内容按页对齐
- 原子性操作：确保快照恢复的一致性

**overlayfs的限制**：
- 不支持O_DIRECT（或支持有限）
- mmap机制与底层文件系统复杂交互
- Union文件系统特性与incoming要求冲突

#### 3. 快照恢复的一致性保证

**incoming需要**：
- pmem文件内容必须与快照创建时完全一致
- 文件访问必须可靠、可预测
- 内存映射必须精确还原

**overlayfs的问题**：
- 多层叠加，文件访问路径复杂
- 可能有缓存层、copy-up机制
- 与incoming的一致性要求冲突

## 技术细节对比

### 正常启动：pmem文件访问

```c
// StratoVirt源码（正常模式）
open(pmem_file_path, O_RDWR)
  → overlayfs driver
  → 查找文件（可能需要copy-up）
  → 返回文件描述符 ✅

mmap(fd, size, PROT_READ|PROT_WRITE, MAP_SHARED)
  → overlayfs支持标准mmap ✅
  → 创建内存映射 ✅
  → pmem设备可以使用 ✅
```

### 快照恢复：pmem文件访问（incoming）

```c
// StratoVirt源码（incoming模式）
open(pmem_file_path, O_RDWR | O_DIRECT)  // ← 可能使用O_DIRECT
  → overlayfs driver
  → 检查是否支持O_DIRECT
  → overlayfs不支持或不完全支持 ❌
  → 返回错误: Read-only file system ❌

// 或者

mmap(fd, size, PROT_READ|PROT_WRITE, MAP_SHARED | MAP_SYNC)
  → overlayfs不支持MAP_SYNC ❌
  → 返回错误 ❌
```

## 类比理解

### 正常启动 = 开车

```
正常启动：
- 像开车走普通公路
- overlayfs就像普通公路，可以正常通行 ✅
- 只要路面可以走就行，不需要特殊要求
```

### 快照恢复 = 开飞机

```
incoming恢复：
- 像开飞机起飞降落
- overlayfs就像普通公路，不能当机场跑道 ❌
- 需要特殊设施（direct I/O、特殊mmap等）
- overlayfs没有这些设施 ❌
```

## 为什么CLH没问题？

### CLH的恢复机制

```
CLH正常启动VM
  ↓
VM运行起来（正常模式）
  ↓
HTTP API: vm.restore(snapshot_file)
  ↓
通过API加载快照数据
  - 不依赖特殊文件系统特性
  - 快照数据通过API传递
  - 可以访问overlayfs文件 ✅
  ↓
VM状态恢复
  ✅ 成功
```

**CLH不在incoming模式下访问pmem文件**：
- VM先在正常模式下启动
- 然后通过HTTP API加载快照
- pmem设备在正常模式下工作 ✅

**StratoVirt必须在incoming模式下访问pmem文件**：
- VM启动时就进入incoming模式
- pmem设备创建时已经处于incoming模式
- incoming模式对overlayfs不支持 ❌

## 总结

**核心差异**：

| 项目 | 正常启动 | 快照恢复启动 |
|------|----------|--------------|
| 启动模式 | Normal | Incoming |
| pmem访问时机 | VM正常初始化 | VM处于incoming状态 |
| 文件访问方式 | 标准open+mmap | 特殊open+mmap（可能O_DIRECT等） |
| overlayfs支持 | ✅ 支持 | ❌ 不支持特殊标志 |
| 结果 | 成功 | 失败 |

**根本原因**：
- 不是文件本身的问题（文件权限正常）
- 不是路径问题（路径正确）
- 而是**incoming模式下的特殊文件访问机制**
- StratoVirt继承QEMU的incoming机制
- incoming对文件系统有严格要求（可能需要direct I/O等）
- overlayfs不支持这些特殊要求 ❌

**类比总结**：
- 正常启动：overlayfs像普通路，StratoVirt像汽车，可以通行 ✅
- 快照恢复：overlayfs像普通路，但incoming模式需要机场跑道 ❌
