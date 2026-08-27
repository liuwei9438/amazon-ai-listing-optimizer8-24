V2.4.5 Image Integration Fixed

基线：用户上传的 amazon-ai-listing-optimizer8-24-main (1).zip

接入内容：
1. 保留/恢复 V1.3.2 主图优化基线（镜像安全判断、角度调整、1600x1600白底、清晰度增强）
2. 新增 image/image_storage.py：Cloudinary HTTPS 上传
3. 新增 image/image_pipeline.py：图片独立适配层，失败返回状态而不抛到文字任务
4. services/concurrent_batch_processor.py：仅勾选 optimize_images 时调用图片链路
5. app.py：新增“优化首图（V1.3.2 稳定基线）”复选框，默认关闭
6. services/listing_exporter.py：仅图片成功时替换原图片列首图；支持仅图片任务导出
7. requirements.txt：增加 Pillow 与 cloudinary

Streamlit Secrets 必须配置：
CLOUDINARY_CLOUD_NAME = "..."
CLOUDINARY_API_KEY = "..."
CLOUDINARY_API_SECRET = "..."

验证：
- python compileall: PASS
- image pipeline import: PASS（Cloudinary使用惰性导入，缺包不会在app启动阶段直接崩溃）
- V1.3.2 本地图像处理 1600x1600: PASS
- image-only Excel 写回：PASS

部署建议：先部署到测试分支，不覆盖 stable-v2.4.4。
