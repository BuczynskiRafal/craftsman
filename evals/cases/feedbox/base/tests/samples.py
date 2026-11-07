"""RSS documents shared by the tests."""

SAMPLE_RSS = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Field Notes</title>
    <item>
      <title>Spring migration</title>
      <link>https://example.org/spring</link>
      <guid>fn-1</guid>
      <pubDate>Mon, 02 Mar 2026 12:00:00 +0000</pubDate>
      <description>&lt;p&gt;Birds are &lt;b&gt;back&lt;/b&gt; early.&lt;/p&gt;</description>
    </item>
    <item>
      <title>Soil samples</title>
      <link>https://example.org/soil</link>
      <pubDate>Tue, 03 Mar 2026 08:30:00 +0100</pubDate>
      <description>Three cores from the north field.</description>
    </item>
    <item>
      <title>Undated note</title>
      <link>https://example.org/undated</link>
    </item>
  </channel>
</rss>
"""
