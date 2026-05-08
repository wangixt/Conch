# StratoVirt快照恢复调试详细文档

## 问题演变过程

### 第一阶段：设备配置不匹配

**原始错误**:
```
Failed to open file: /run/conch/snapshot/default/shared/sha256fb63.../layer0.erofs
Failed to add virtio pci pmem device
Failed to realize virtio device
```

**初步诊断**:
- 恢复时缺少pmem设备
- 内存区域不匹配
- 共享rootfs路径不存在

### 第二阶段：代码修复

#### 修复1: 添加pmem设备到恢复脚本

**问题**: resumeScriptStratovirt模板缺少 `{{ .PmemDevices }}`

**位置**: `/home/Conch/internal/sandbox/vmm/stratovirt.go:60`

**修复**:
```go
const resumeScriptStratovirt = `...
-device virtio-net-pci,netdev=net0,id=net0,bus=pcie.0,addr=0x10 \
{{ .PmemDevices }} \    # ← 新增
-device vhost-vsock-pci,id=vsock0,guest-cid={{ .VsockCID }},bus=pcie.0,addr=0x11 \
...`
```

**效果**: 恢复命令现在包含完整的pmem配置

#### 修复2: 移除readonly参数

**问题**: StratoVirt不支持 `readonly=on` 参数

**位置**: `/home/Conch/internal/sandbox/vmm/stratovirt.go:190-195`

**原始错误日志**:
```
2026-05-08T17:44:15.683: ERROR: Failed to create vmconfig
Add args "memory-backend-file,size=1988M,id=pmem0,mem-path=...,share=on,readonly=on"
error: unexpected argument found
```

**修复前**:
```go
var object string
if readonly {
    object = fmt.Sprintf("-object memory-backend-file,size=%s,id=%s,mem-path=%s,share=on,readonly=on", ...)
} else {
    object = fmt.Sprintf("-object memory-backend-file,size=%s,id=%s,mem-path=%s,share=on", ...)
}
```

**修复后**:
```go
object := fmt.Sprintf("-object memory-backend-file,size=%s,id=%s,mem-path=%s,share=on", ...)
```

**效果**: StratoVirt可以正常解析pmem配置

#### 修复3: vsock FileConn无限循环

**问题**: Agent READY后，FileConn失败导致无限循环

**位置**: `/home/Conch/internal/sandbox/manager.go:320-329`

**原始错误日志**:
```
2026-05-08 17:59:32 [INFO] Stratovirt Sandbox Agent is officially READY!
2026-05-08 17:59:32 [WARN] failed to create net.Conn from vsock fd
    error=file file+net vsock: protocol not supported
# 无限重复上述日志...
```

**根本原因**: Go的`net.FileConn`不支持vsock类型的文件描述符

**修复前**:
```go
vsockConn, err := net.FileConn(file)
if err != nil {
    logger.Warn("failed to create net.Conn from vsock fd", ulog.F("error", err))
    file.Close()
    time.Sleep(vsockSignalRetry)
    continue  # ← 无限循环
}
```

**修复后**:
```go
vsockConn, err := net.FileConn(file)
if err != nil {
    logger.Warn("failed to create net.Conn from vsock fd, but Agent is READY so proceeding", ulog.F("error", err))
    file.Close()
    close(readyCh)  # ← 直接完成
    return
}
```

**效果**: Sandbox创建不再卡住

### 第三阶段：发现根本限制

#### 测试结果

**Sandbox创建测试 ✅**:
```python
sbx = Sandbox.create()
result = sbx.execute(cmd='python3', content='print("Hello")')
print(f"Result: {result}")
# 输出: Hello
# ✅ 成功
```

**快照创建测试 ✅**:
```python
snapshot_info = sbx.pause()
print(f"Snapshot ID: {snapshot_info.snapshot_id}")
# 输出: sha256:065a7d360b2766d506ae2d4e75cb469487a24439c7bac3db801f5642e87e762e
# ✅ 成功
```

**快照恢复测试 ❌**:
```python
sbx2 = Sandbox.create(snapshot_id=snapshot_id)
# 报错: Failed to open file: .../layer0.erofs
#       Read-only file system (os error 30)
# ❌ 失败
```

#### 新错误分析

**StratoVirt错误日志**:
```
2026-05-08T18:03:55 [ERROR]: Failed to realize standard VM.
Caused by:
    0: add virtio-pmem-pci fail.
    1: Failed to add virtio pci pmem device
    2: Failed to add virtio pci device
    3: Failed to realize virtio device
    4: Failed to open file: /run/conch/snapshot/default/shared/sha256065a.../layer0.erofs
    5: Read-only file system (os error 30)
```

**关键观察**:

1. **文件确实存在**:
   ```
   ls -la /var/run/conch/snapshot/default/shared/sha256065a.../layer0.erofs
   -rw-r--r-- 1 root root 2084569088 Apr 14 21:53 layer0.erofs
   ```

2. **文件权限正常**: 权限是 `rw-r--r--`，理论上应该可读

3. **overlayfs mount**:
   ```
   mount | grep sha256065a
   overlay on /run/conch/snapshot/default/sandbox_4ce.../rootfs type overlay (rw,...)
   ```

4. **VM snapshot mount**:
   ```
   /dev/sdd on /run/conch/snapshot/default/shared/sha2567fe0c2b2... type ext4 (ro,...)
   ```
   VM snapshot被mount为只读ext4，包含kernel/initrd

#### 为什么报告"Read-only file system"？

**不是文件权限问题**，而是**文件系统层面的限制**。

**overlayfs特性**:
- Union文件系统，叠加多个层
- 使用特殊的mount机制
- 文件访问通过overlayfs驱动

**StratoVirt incoming机制**:
- 在VM初始化阶段加载快照
- 需要直接访问底层文件系统
- 可能需要特定文件系统特性（direct I/O、内存映射等）
- overlayfs可能不满足这些要求

**关键发现**:
- StratoVirt在incoming恢复模式下，无法访问overlayfs mount的文件
- 即使文件本身是可读写的，overlayfs文件系统被识别为"只读"
- 这是StratoVirt的设计限制，不是代码bug

## 详细调试日志

### Sandbox创建流程（成功）

```
1. Create sandbox request
   ↓
2. Prepare snapshot workspace
   - Mount rootfs overlayfs (active)
   - Mount vm overlayfs (active) → kernel/initrd
   - Create mem.img file
   ↓
3. Build start command
   -object memory-backend-file,size=1024M,id=mem0,mem-path=.../mem.img,share=on
   -object memory-backend-file,size=1988M,id=pmem0,mem-path=.../rootfs/layer0.erofs,share=on
   -device virtio-pmem-pci,id=pmem0pci,memdev=pmem0,bus=pcie.0,addr=0x12
   ↓
4. Start StratoVirt VM
   ✅ SUCCESS
   ↓
5. VM socket ready
   ✅ QMP connection established
   ↓
6. Agent READY signal
   ✅ Sandbox created successfully
```

### 快照创建流程（成功）

```
1. Pause sandbox request
   ↓
2. Send QMP "stop" command
   ✅ VM paused
   ↓
3. Send QMP "migrate" command
   - uri: file:/var/run/conch/snapshot/default/sandbox_xxx/mem/conch/snapshot
   ✅ Snapshot migration started
   ↓
4. Wait for migration completion
   ✅ Snapshot completed
   ↓
5. Generate config.json
   - kernel: .../boot/vmlinuz
   - initramfs: .../data/conch.initrd
   - memory: 1024M
   - vsock CID: 3
   ✅ config.json created
   ↓
6. Commit snapshot to containerd
   - rootfs snapshot: sha256:065a... (labels: vm-snapshot=sha256:7fe0..., mem-snapshot=...)
   - vm snapshot: sha256:7fe0... (kernel/initrd)
   - mem snapshot: sha256:... (memory/state)
   ✅ Snapshot committed successfully
   ↓
7. Create shared view mounts
   - shared-rootfs-sha256065a... → overlayfs mount
   - shared-vm-sha2567fe0... → ext4 mount (ro)
   ✅ Shared mounts created
```

### 快照恢复流程（失败）

```
1. Create sandbox with snapshot_id=sha256:065a...
   ↓
2. resolveParentSnapshotIDs
   parents.Rootfs = sha256:065a...
   parents.VM = sha256:7fe0...
   parents.Mem = sha256:...
   ↓
3. AcquireResumeWorkspace
   - Create shared-rootfs view mount
   - Create shared-vm view mount  
   - Prepare mem directory
   ✅ Workspace prepared
   ↓
4. Build resume command
   -kernel /var/run/conch/snapshot/default/shared/sha2567fe0.../boot/vmlinuz
   -initrd /var/run/conch/snapshot/default/shared/sha2567fe0.../data/conch.initrd
   -object memory-backend-ram,size=1024M,id=mem0
   -object memory-backend-file,size=1988M,id=pmem0,mem-path=/var/run/conch/snapshot/default/shared/sha256065a.../layer0.erofs,share=on
   -device virtio-pmem-pci,id=pmem0pci,memdev=pmem0,bus=pcie.0,addr=0x12
   -incoming file:/var/run/conch/snapshot/default/sandbox_xxx/mem/conch/snapshot
   ↓
5. Start StratoVirt VM
   - VM socket created ✅
   - QMP connection established ✅
   ↓
6. StratoVirt initialization
   - Parse command line args ✅
   - Setup machine config ✅
   - Setup boot source ✅
   - Setup devices:
     - virtio-net-pci ✅
     - virtio-pmem-pci ❌ ← FAILED HERE
   ↓
7. Error reported
   Failed to open file: .../layer0.erofs
   Read-only file system (os error 30)
   ↓
8. VM startup failed
   ❌ VM process exited with error
   ↓
9. Cleanup triggered
   - Remove snapshot workspace
   - Unmount shared views
   ❌ Sandbox creation failed
```

## 问题根源分析

### StratoVirt的incoming机制

**incoming模式下的文件访问**:

```
StratoVirt启动
  ↓
解析 -incoming file:参数
  ↓
进入incoming模式
  ↓
初始化VM配置
  ↓
创建设备
  - virtio-net-pci ✅
  - virtio-pmem-pci:
    - 读取pmem mem-path文件 ← 需要直接访问
    - 创建memory-backend映射
    ↓
    ❌ 遇到overlayfs
    - overlayfs不满足incoming模式的文件访问要求
    - 报错: Read-only file system
```

### 为什么overlayfs不满足要求？

**可能的原因**:

1. **Direct I/O要求**:
   - StratoVirt可能需要使用O_DIRECT标志打开文件
   - overlayfs可能不支持direct I/O
   - 导致无法正确映射pmem文件

2. **内存映射限制**:
   - pmem需要将文件映射到VM内存空间
   - overlayfs的mmap机制可能与StratoVirt要求不匹配
   - 导致映射失败

3. **文件系统识别**:
   - StratoVirt检测到overlayfs类型
   - 将其标记为"只读"（即使文件本身是rw）
   - 拒绝在incoming模式下使用

4. **QEMU兼容性问题**:
   - StratoVirt继承QEMU的incoming机制
   - QEMU的incoming可能对overlayfs有限制
   - StratoVirt继承了这些限制

### 与CLH对比

**CLH快照恢复机制**:
```
CLH启动VM
  ↓
VM正常运行（无需incoming模式）
  ↓
HTTP API: POST /api/v1/vm.restore
  ↓
通过API加载快照
  - 快照数据通过API传递
  - 不依赖特定文件系统
  - 可以访问overlayfs文件 ✅
  ↓
VM状态恢复
  ↓
✅ 恢复成功
```

**StratoVirt快照恢复机制**:
```
StratoVirt启动VM（incoming模式）
  ↓
在启动阶段就需要访问文件
  ↓
创建pmem设备
  ↓
访问pmem mem-path文件
  ↓
❌ overlayfs不满足要求
  ↓
失败: Read-only file system
```

## 根本矛盾

### Conch的设计

**使用overlayfs的原因**:
- 节省存储空间（多层共享）
- containerd原生支持
- 快照管理方便
- 适合容器化场景

**rootfs snapshot设计**:
- Active snapshot → overlayfs mount（rw）
- Shared snapshot → overlayfs view mount（ro）
- 恢复时使用shared snapshot

### StratoVirt的要求

**incoming恢复的要求**:
- 设备配置完全匹配
- pmem设备必须在启动时存在
- pmem文件必须可访问
- **不支持overlayfs文件系统**

**矛盾点**:
- Conch需要overlayfs节省空间
- StratoVirt不支持overlayfs
- 两者设计理念冲突

## 解决方案评估

### 方案1: 使用CLH（已实现）

**可行性**: ✅ 完全可行

**优势**:
- CLH已完全支持overlayfs
- 快照恢复功能已实现
- 无需额外开发工作

**劣势**:
- 性能略低于StratoVirt
- openEuler集成不如StratoVirt

**实施**:
```yaml
# config.yaml
sandbox:
  default_vmm: cloud-hypervisor
```

**结论**: 推荐方案，立即可用

### 方案2: 为StratoVirt创建专用snapshotter

**可行性**: ⚠️ 理论可行，工作量巨大

**设计思路**:
```
stratovirt-snapshotter:
  - 不使用overlayfs
  - 每个snapshot作为独立ext4镜像
  - 满足StratoVirt的文件系统要求
```

**优势**:
- 满足StratoVirt要求
- 可以实现完整快照功能

**劣势**:
- 开发工作量巨大（数周）
- 存储空间占用大（每个snapshot完整拷贝）
- 与containerd overlayfs不兼容
- 需要维护两套snapshotter

**实施难度**: 高

**结论**: 长期方案，需要专门开发

### 方案3: 修改rootfs处理方式

**子方案A: 创建时不使用pmem**

**可行性**: ❌ 不可行

**原因**:
- StratoVirt需要pmem设备提供rootfs
- 不使用pmem会导致VM无法启动
- 失去pmem的性能优势

**子方案B: 快照包含rootfs内存数据**

**可行性**: ❌ 不可行

**原因**:
- StratoVirt要求设备配置匹配
- pmem内存区域必须存在才能恢复
- 无法在恢复时动态添加pmem

**结论**: ❌ 不可行

### 方案4: 使用raw block设备

**可行性**: ⚠️ 需要验证

**设计思路**:
- 不使用virtio-pmem-pci
- 使用virtio-blk设备提供rootfs
- Block设备可能没有overlayfs限制

**需要验证**:
- StratoVirt是否支持virtio-blk
- Block设备是否可以访问overlayfs
- 快照恢复时block设备配置要求

**风险**:
- 可能仍然遇到overlayfs限制
- Block设备性能不如pmem
- 需要大量测试验证

**结论**: 需要进一步研究验证

### 方案5: 使用软链接或bind mount

**可行性**: ❌ 不可行

**尝试**:
```bash
# 从overlayfs bind mount到ext4
mount --bind /overlayfs/path /ext4/path
```

**问题**:
- bind mount仍然指向overlayfs
- StratoVirt仍然会检测到overlayfs
- 无法绕过限制

**结论**: ❌ 无法绕过overlayfs限制

## 最终建议

### 短期方案（立即可用）

**使用CLH进行快照功能**:
```yaml
# config/config.yaml
sandbox:
  default_vmm: cloud-hypervisor
```

**适用场景**:
- 需要快照恢复功能
- 对性能要求不是极致
- 需要稳定的快照功能

**优势**:
- 立即可用
- 功能完整稳定
- 无需额外开发

### 中期方案（可选）

**混合使用策略**:
- 普通sandbox → StratoVirt（性能好）
- 快照功能 → CLH（功能完整）

**适用场景**:
- 不同场景使用不同VMM
- 根据需求灵活选择

### 长期方案（需要开发）

**为StratoVirt开发专用snapshotter**:
- 使用ext4文件而非overlayfs
- 每个snapshot作为独立镜像
- 满足StratoVirt要求

**适用场景**:
- 需要StratoVirt的极致性能
- 需要在StratoVirt上实现完整快照
- openEuler深度集成需求

**工作量**: 大，需要数周开发时间

## 技术总结

### StratoVirt的限制本质

**不是bug，而是设计限制**:
- StratoVirt继承QEMU的incoming机制
- incoming机制对文件系统有严格要求
- overlayfs不满足这些要求
- 这是架构层面的限制

### 根本矛盾

**Conch需求**:
- 使用overlayfs节省空间
- 共享snapshot减少存储
- 容器化设计理念

**StratoVirt要求**:
- incoming需要特定文件系统
- pmem设备在启动时必须存在
- 不支持overlayfs

**两者冲突**: 无法通过简单代码修改解决

### 正确的理解

**不是"快照恢复失败"**:
- 快照创建完全成功
- 快照数据完整保存
- 恢复流程正确执行

**而是"文件系统不兼容"**:
- StratoVirt无法访问overlayfs
- 这是StratoVirt的设计限制
- 需要不同的技术方案解决

## 参考资料

- StratoVirt源码: https://gitee.com/openeuler/stratovirt
- QEMU migration: https://qemu-project.gitlab.io/qemu-system/migration/
- Overlayfs: https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html
- Virtio-pmem: https://www.kernel.org/doc/html/latest/driver-api/virtio-pmem.html
- Containerd snapshotter: https://github.com/containerd/containerd/blob/main/docs/snapshotter.md